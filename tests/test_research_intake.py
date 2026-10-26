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
    def test_ordinary_outcomes_loads_primitives_and_delivers_chinese_markdown(self):
        request = '仅依据所给材料整理 Markdown 摘要，不生成 PDF。'
        content = '# 材料摘要\n单次观察不能证明稳定性。\n'
        raw = {'write_file', 'edit_file', 'run_process'}
        permitted_raw = raw - {'run_process'}
        client = _JSONClient([
            _reply('load_tools', {'names': sorted(permitted_raw)}),
            _reply('write_file', {'path': 'workspace/summary.md', 'content': content}),
            _reply('deliver_result', {'summary': '已交付材料摘要。',
                'artifacts': ['workspace/summary.md'], 'unresolved': []}),
        ])
        services = ResearchApplicationServices(llm_client=client,
            config={'research_materials_only': True},
            budget_limits={'llm_requests': 8, 'total_tokens': 1000000})
        with TemporaryDirectory() as directory:
            base = Path(directory)
            note = base / 'notes.md'
            note.write_text('单次观察不能证明稳定性。', encoding='utf-8')
            original = note.read_bytes()
            app = create_research_task(ResearchBrief(request_text=request,
                requested_outputs=('assessment',), asset_requests=({'locator': str(note)},)),
                root=base / 'task', services=services)
            self.assertEqual(app.state['config']['tool_contracts'], 'outcomes')
            permissions = set(app.gateway.policy.allowed_permissions)
            self.assertEqual(set(app._specialist_catalog()), permitted_raw)
            result = app.run(progress=lambda _: None)
            self.assertEqual(result['status'], 'completed', result)
            first = {row['function']['name'] for row in client.calls[0][1]['tools']}
            self.assertTrue({'prepare_implementation', 'read_file', 'list_directory',
                'ask_user', 'deliver_result', 'inspect_artifacts'} <= first)
            self.assertFalse(raw & first)
            second = {row['function']['name'] for row in client.calls[1][1]['tools']}
            self.assertTrue(permitted_raw <= second)
            self.assertEqual(app.gateway.policy.allowed_permissions, permissions)
            observations = [app.session.store.read_json(item['result'])
                for turn in app.state['turns'] for item in turn['tools']]
            self.assertEqual([row['tool_name'] for row in observations],
                ['load_tools', 'write_file', 'deliver_result'])
            self.assertTrue(all(row['status'] == 'ok' for row in observations), observations)
            self.assertEqual((base / 'task/workspace/summary.md').read_text(encoding='utf-8'), content)
            self.assertEqual(note.read_bytes(), original)
            self.assertIsNone(app.state['config'].get('report_requirement'))
            self.assertFalse(list((base / 'task/workspace').rglob('*.pdf')))

    def test_operation_contract_modes_and_saved_defaults_preserve_authority(self):
        raw = {'write_file', 'edit_file', 'run_process'}
        names = lambda app: {row['function']['name'] for row in app.schemas}
        with TemporaryDirectory() as directory:
            base = Path(directory)
            preview, _, _, _ = ToolApplication._configuration(base / 'preview', 'Preview',
                model=None, sources={})
            self.assertEqual(preview['tool_contracts'], 'outcomes')
            for mode in ('outcomes', 'full', 'on_demand', None):
                with self.subTest(mode=mode):
                    services = ResearchApplicationServices(llm_client=_JSONClient())
                    app = ToolApplication.create(base / str(mode), 'Ordinary work', model='fixture',
                        sources={}, allow_execution=True, services=services,
                        tool_contracts=mode if mode is not None else 'full')
                    if mode is None:
                        app.state['config'].pop('tool_contracts')
                        app._save()
                    expected = names(app)
                    allowed = {spec.name for spec in app.gateway.list_specs()
                        if app.gateway.policy.is_allowed(spec)[0]}
                    if mode == 'outcomes':
                        self.assertEqual(expected, allowed - raw)
                        self.assertEqual(set(app._specialist_catalog()), raw)
                        self.assertTrue({'execute_implementation', 'export_report',
                            'read_file', 'inspect_artifacts'} <= expected)
                    elif mode in ('full', None):
                        self.assertEqual(expected, allowed)
                    else:
                        self.assertEqual(expected, {name for name in allowed
                            if app.gateway.registry.request_visible(name)})
                        self.assertTrue(raw <= expected)
                    saved_config = copy.deepcopy(app.state['config'])
                    saved_bytes = (app.session.store.root / 'actions.json').read_bytes()
                    restored = ToolApplication.load(app.session.store.root, services=services)
                    self.assertEqual(restored.state['config'], saved_config)
                    self.assertEqual(names(restored), expected)
                    self.assertEqual((app.session.store.root / 'actions.json').read_bytes(), saved_bytes)
                    if mode == 'outcomes':
                        permissions = set(restored.gateway.policy.allowed_permissions)
                        ledger = restored.ledger.to_dict()
                        restored.run(actions=[
                            {'tool_name': 'load_tools', 'arguments': {'names': sorted(raw)}},
                            {'tool_name': 'ask_user', 'arguments': {'question': 'Which operation next?'}}],
                            progress=lambda _: None)
                        self.assertTrue(raw <= names(restored))
                        self.assertEqual(restored.gateway.policy.allowed_permissions, permissions)
                        self.assertEqual(restored.ledger.to_dict(), ledger)
                        self.assertEqual(names(ToolApplication.load(restored.session.store.root,
                            services=services)), names(restored))
            denied = ToolApplication.create(base / 'denied', 'No execution', model=None, sources={})
            denied.run(actions=[{'tool_name': 'load_tools', 'arguments': {'names': ['run_process']}}],
                progress=lambda _: None)
            receipt = denied.session.store.read_json(denied.state['turns'][0]['tools'][0]['result'])
            self.assertEqual(receipt['status'], 'blocked')
            self.assertNotIn('run_process', names(denied))
            self.assertNotIn('run_process', denied._specialist_catalog())

    def test_native_default_attempts_do_not_inherit_legacy_stage_units(self):
        from simple_ar.app.research_application import create_session, load_session
        with TemporaryDirectory() as directory:
            root = Path(directory)
            note = root / 'note.md'
            note.write_text('A supplied observation, not a verified conclusion.', encoding='utf-8')
            brief = ResearchBrief(request_text='Assess the supplied observation.',
                requested_outputs=('assessment',), asset_requests=({'locator': str(note)},))
            client = _JSONClient()
            services = ResearchApplicationServices(llm_client=client,
                config={'research_materials_only': True})
            self.assertIsNone(services.max_attempts)
            app = create_research_task(brief, root=root / 'native', services=services)
            self.assertEqual(app.state['config']['context_protocol'],'working.v1')
            self.assertEqual(app.session.manifest.budget.max_attempts, 300)
            actions = [{'tool_name': 'write_file', 'arguments': {
                'path': f'workspace/note-{index}.md', 'content': 'Observed evidence only.'}}
                for index in range(17)]
            actions.append({'tool_name': 'ask_user', 'arguments': {
                'question': 'Which scientific interpretation should the next comparison address?'}})
            app.run(actions=actions, progress=lambda _: None)
            self.assertTrue((root / 'native/workspace/note-16.md').is_file())
            self.assertEqual(len(app.session.list_attempts()), 18)
            self.assertEqual(client.calls, [])
            self.assertEqual(app.ledger.entries, ())
            for name, options, expected in (
                    ('bounded-model', replace(services, budget_limits={'llm_requests': 4, 'total_tokens': 100000}), 40),
                    ('explicit', replace(services, max_attempts=7), 7),
                    ('caller', replace(services, llm_client=None), 16)):
                with self.subTest(creation=name):
                    created = create_research_task(brief, root=root / name, services=options)
                    self.assertEqual(created.session.manifest.budget.max_attempts, expected)
                    restored = ToolApplication.load(root / name, services=replace(options, max_attempts=999))
                    self.assertEqual(restored.session.manifest.budget.max_attempts, expected)
            experiments = create_research_task(replace(brief, requested_outputs=('experiments',)),
                root=root / 'experiments', services=services)
            self.assertEqual(experiments.session.manifest.budget.max_attempts, 300)
            self.assertEqual(experiments.ledger.remaining('process_invocations'), 16)
            legacy = create_session(brief, root=root / 'legacy', services=replace(services, llm_client=None))
            self.assertEqual(legacy.services.max_attempts, 16)
            restored = load_session(root / 'legacy', services=ResearchApplicationServices(max_attempts=999))
            self.assertEqual(restored.services.max_attempts, 16)
            self.assertEqual(restored.controller.manifest.budget.max_attempts, 16)
            for invalid in (0, -1, True, 1.5, '16'):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    ResearchApplicationServices(max_attempts=invalid)

    def test_deferred_experiment_retains_materials_then_supplies_execution_in_same_task(self):
        import sys
        from simple_ar.tools.scientific import measurement_contract
        with TemporaryDirectory() as directory, patch.object(LLMClient, 'from_env',
                side_effect=AssertionError('Collection and caller measurements need no model')):
            base = Path(directory).resolve()
            notes = base / 'notes.md'
            notes.write_text('# Conditions\nOne observation does not establish stability.\n', encoding='utf-8')
            original_notes = notes.read_bytes()
            brief = ResearchBrief(request_text='Collect these conditions, then measure the result.',
                requested_outputs=('experiments',),
                asset_requests=({'locator': str(notes), 'role': 'paper'},))
            app = create_research_task(brief, root=base / 'task', services=ResearchApplicationServices(
                max_attempts=30, config={'research_materials_only': True},
                budget_limits={'process_invocations': 1, 'process_wall_seconds': 5}))
            identity = app.session.manifest.session_id
            self.assertIsNone(app.state['config']['model'])
            self.assertIsNone(app.state['config'].get('execution_contract'))
            self.assertTrue(app.sources.policy.require_design)
            app.run(actions=[{'tool_name': 'research', 'arguments': {'operation': 'summary',
                'question': brief.request_text, 'materials': ['source0'], 'use_llm': False}}], progress=lambda _: None)
            collected = app.session.store.read_json(app.state['turns'][-1]['tools'][0]['result'])
            self.assertEqual(collected['status'], 'ok', collected.get('error'))
            retained = {path: app.workspace.resolve(path).read_bytes() for path in collected['artifacts']}
            self.assertTrue(app.sources.delivery_issues([]))
            original_ledger = app.ledger.to_dict()
            contract = measurement_contract({'command': [sys.executable, '-c', "print('score: 3')"],
                'cwd': str(base), 'timeout_sec': 5, 'result_schema': {'primary_metric': 'score'}})
            app = ToolApplication.load(base / 'task')
            app.supply(supply_id='prepared-command', reason='Caller supplied the missing measurement',
                changes={'execution_contract': contract})
            self.assertEqual(app.session.manifest.session_id, identity)
            self.assertEqual(app.ledger.to_dict(), original_ledger)
            self.assertEqual({path: app.workspace.resolve(path).read_bytes() for path in retained}, retained)
            decision = app.request_execution_confirmation()
            app.run(decision={'decision_id': decision['decision_id'], 'decision_response': 'accept'},
                progress=lambda _: None)
            app.run(actions=[{'tool_name': 'measure_experiment', 'arguments': contract}], progress=lambda _: None)
            self.assertFalse(app.ledger.entries)
            token = app.approval_token(app.state['pending']['call'])
            app.run(approved_execution=token, progress=lambda _: None)
            measured = app.session.store.read_json(app.state['turns'][-1]['tools'][0]['result'])
            self.assertEqual(measured['status'], 'ok', measured.get('error'))
            self.assertEqual(measured['data']['metrics']['score'], 3)
            self.assertEqual(sum(row.actual.get('process_invocations', 0) for row in app.ledger.entries), 1)
            self.assertEqual(notes.read_bytes(), original_notes)

    def test_open_execution_does_not_freeze_omitted_comparison_as_skip(self):
        import sys
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            execution = {'command': [sys.executable, '-c', "print('score: 3')"], 'cwd': str(base),
                'timeout_sec': 5, 'result_schema': {'primary_metric': 'score'}}
            app = create_research_task(ResearchBrief(request_text='Investigate a justified comparison.',
                requested_outputs=('experiments',)), root=base / 'task',
                services=ResearchApplicationServices(config={'execution': execution,
                    'require_design': True, 'research_materials_only': True}, max_attempts=9))
            self.assertIsNone(app.scientific.execution_contract)
            self.assertNotIn('baseline_policy', app.sources.policy.execution_boundary)
            self.assertEqual(app.sources.policy.execution_boundary['command'], execution['command'])
            self.assertEqual(app.ledger.remaining('process_invocations'), 9)
            self.assertTrue(app.scientific.execution_issues())
            self.assertEqual(app.session.list_attempts(), ())

    def test_json_client_resumes_one_native_task_delivers_and_terminal_load_is_idle(self) -> None:
        content = "# Assessment\nCalibration error 0.12 alone cannot establish robustness.\n"
        request = "Assess the observation: calibration error is 0.12."
        # This request asks for an assessment, not a file or formal report.
        # The common loop can directly deliver this assessment; no compulsory
        # report-intent declaration or Writer stage is needed.
        final_reply = _reply("deliver_result", {"summary": content,
            "artifacts": [], "unresolved": []})
        client = _JSONClient([final_reply])
        services = ResearchApplicationServices(llm_client=client,
            budget_limits={"llm_requests": 1, "total_tokens": 1000000},
            config={"research_materials_only": True})
        brief = ResearchBrief(request_text=request,
            requested_outputs=("assessment",))
        with TemporaryDirectory() as directory, patch.object(LLMClient, "from_env",
                side_effect=AssertionError("Injected services must not use environment clients")):
            root = Path(directory) / "task"
            app = create_research_task(brief, root=root, services=services, model=None)
            self.assertIsInstance(app, ToolApplication)
            identity = app.session.manifest.session_id
            self.assertEqual(app.session.manifest.transition_recipe, ToolApplication.RECIPE)
            self.assertEqual(app.state['config']['control_protocol'], 'operations.v1')
            self.assertIsNone(app.writing)
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
            observations = [resumed.session.store.read_json(item['result'])
                for turn in resumed.state['turns'] for item in turn['tools']]
            self.assertEqual([row['tool_name'] for row in observations],
                ['deliver_result'])
            self.assertTrue(all(row['status'] == 'ok' for row in observations), observations)
            self.assertIsNone(resumed.state['config'].get('report_requirement'))
            self.assertEqual(result["session_id"], identity)
            delivery = json.loads((root / "delivery.json").read_text(encoding="utf-8"))
            self.assertEqual(delivery["summary"], content)
            self.assertEqual(delivery["artifacts"], [])
            self.assertFalse(any((root / 'workspace').iterdir()))
            self.assertEqual(len(client.calls), 1)
            entries = resumed.ledger.entries
            self.assertEqual(len(entries), 1)
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
            self.assertEqual(len(client.calls), 1)
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
            catalog.write_text('version = 1\n[routes]\ndefault = "acceptance"\nvision = "visual"\n'
                '[profiles.acceptance]\napi = "openai_chat"\n'
                'base_url = "https://acceptance.invalid/v1"\nmodel = "fixture-model"\n'
                'api_key_env = "ACCEPTANCE_MODEL_KEY"\ncapabilities = ["text"]\n'
                '[profiles.visual]\napi = "openai_chat"\n'
                'base_url = "https://acceptance.invalid/v1"\nmodel = "fixture-vision"\n'
                'api_key_env = "ACCEPTANCE_MODEL_KEY"\ncapabilities = ["vision"]\n', encoding="utf-8")
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
                figure = root / "figure.svg"
                figure.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
                visual_brief = ResearchBrief(request_text="Inspect the supplied image and explain its evidence.",
                    requested_outputs=("assessment",),
                    asset_requests=({'locator': str(figure), 'role': 'material'},))
                visual_services = ResearchApplicationServices(config={'research_materials_only': True},
                    budget_limits={'llm_requests': 2, 'total_tokens': 1000,
                        'process_invocations': 0, 'process_wall_seconds': 0, 'source_operations': 0})
                for label, options in (('omitted', {}), ('denied', {'allow_vision': False}),
                        ('authorized', {'allow_vision': True})):
                    with self.subTest(vision=label), patch.object(ToolApplication, 'create',
                            wraps=ToolApplication.create) as create:
                        visual = create_research_task(visual_brief, root=root / label,
                            model='profile:acceptance', services=visual_services, **options)
                        create.assert_called_once()
                        granted = options.get('allow_vision', False)
                        self.assertIs(create.call_args.kwargs['allow_vision'], granted)
                        self.assertIsInstance(visual, ToolApplication)
                        config = visual.state['config']
                        self.assertIs(config['allow_vision'], granted)
                        if granted:
                            self.assertEqual(config['vision_connection']['profile'], 'visual')
                            self.assertEqual(config['vision_connection']['settings']['model'], 'fixture-vision')
                        else:
                            self.assertIsNone(config['vision_connection'])
                        self.assertFalse(config['allow_network'])
                        self.assertFalse(config['allow_execution'])
                        self.assertEqual(config['max_images'], 0)
                        for resource, limit in visual_services.budget_limits.items():
                            self.assertEqual(visual.ledger.remaining(resource), limit)
                        self.assertEqual(visual.session.list_attempts(), ())
                        self.assertEqual(visual.ledger.entries, ())
                        restored = ToolApplication.load(root / label)
                        self.assertIs(restored.state['config']['allow_vision'], granted)
                        self.assertEqual(restored.state['config']['vision_connection'], config['vision_connection'])
                for invalid in (None, 0, 1, 'true', 'false', [], {}):
                    with self.subTest(allow_vision=invalid), patch.object(ToolApplication, 'create') as create, \
                            patch.object(LLMClient, 'from_env') as connection:
                        with self.assertRaisesRegex(ValueError, 'allow_vision must be a bool'):
                            create_research_task(visual_brief, root=root / 'invalid-vision',
                                model='profile:acceptance', allow_vision=invalid)
                        create.assert_not_called()
                        connection.assert_not_called()
                        self.assertFalse((root / 'invalid-vision').exists())
                calls.assert_not_called()


    def test_public_minimal_code_creation_uses_code_task_defaults_and_runtime_services(self):
        from simple_ar.app.research_execution import code_task_execution_options
        from simple_ar.code_task.runtime.config import load_code_task_init_options, load_code_task_execute_options
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            project = base / 'project'
            project.mkdir()
            (project / 'check.py').write_text('pass\n', encoding='utf-8')
            client, reviewer = _JSONClient(), _JSONClient()
            registry = SearchProviderRegistry()
            task = {'code_root': 'project', 'validation_command': ['python', 'check.py'],
                'validation_timeout_sec': 7}
            execution = {'command': ['python', 'measure.py'], 'cwd': 'project', 'timeout_sec': 9,
                'result_schema': {'primary_metric': 'score'}, 'baseline_policy': 'skip', 'code_task': task}
            original = copy.deepcopy(execution)
            services = ResearchApplicationServices(llm_client=client, feasibility_llm_client=reviewer,
                search_registry=registry, input_base_dir=base,
                config={'execution': execution, 'require_design': False},
                budget_limits={'llm_requests': 2, 'total_tokens': 100000,
                    'process_invocations': 0, 'process_wall_seconds': 0, 'source_operations': 0})
            with patch.object(LLMClient, 'from_env', side_effect=AssertionError('Use supplied services')):
                app = create_research_task(ResearchBrief(request_text='Implement an adapter.',
                    requested_outputs=('experiments',)), root=base / 'task', services=services)
            saved = app.state['config']
            defaults = code_task_execution_options(load_code_task_init_options(code_root=str(project),
                require_task_file=False), load_code_task_execute_options())
            exact = saved['code_task']
            self.assertEqual(exact['preparation']['kind'], defaults['kind'])
            self.assertEqual(exact['preparation']['workspace_mode'], defaults['workspace_mode'])
            self.assertEqual(exact['preparation']['max_repairs'], defaults['max_repairs'])
            self.assertEqual(exact['allowed_patterns'], defaults['allowed_patterns'] or ['**'])
            from simple_ar.code_task.editing.scope import is_edit_allowed_path
            for path in ('adapter.py', 'src/model.py', 'README.md', 'tests/test_adapter.py', '.env'):
                self.assertEqual(is_edit_allowed_path(path, allowed_patterns=exact['allowed_patterns'],
                    protected_patterns=exact['protected_patterns']),
                    is_edit_allowed_path(path, allowed_patterns=defaults['allowed_patterns'],
                        protected_patterns=defaults['protected_patterns']), path)
            self.assertEqual(exact['project_path'], 'source0')
            self.assertEqual(exact['validation_command'], ['python', 'check.py'])
            self.assertEqual(saved['execution_contract']['conditions'][0]['source'], 'implementation')
            self.assertEqual(saved['execution_contract']['conditions'][0]['execution']['command'][1], 'measure.py')
            self.assertIs(app.services, services)
            self.assertIs(app.sources.registry, registry)
            self.assertFalse(saved['allow_network'])
            self.assertEqual(app.ledger.remaining('process_invocations'), 0)
            self.assertEqual(app.ledger.remaining('process_wall_seconds'), 0)
            self.assertEqual(app.ledger.remaining('source_operations'), 0)
            self.assertEqual(app.session.list_attempts(), ())
            self.assertEqual(client.calls, [])
            self.assertEqual(reviewer.calls, [])
            self.assertEqual(execution, original)
            self.assertEqual((project / 'check.py').read_text(), 'pass\n')
            paper = base / 'method.md'
            paper.write_text('A supplied method description, not verified implementation evidence.', encoding='utf-8')
            for outputs in (('code_task', 'summary'), ('code_task', 'experiments')):
                combined = create_research_task(ResearchBrief(request_text='Use the supplied method and implement it.',
                    requested_outputs=outputs, asset_requests=({'locator': str(paper), 'role': 'paper'},)),
                    root=base / '-'.join(outputs), services=services)
                self.assertEqual(combined.state['config']['code_task']['project_path'], 'source0')
                self.assertEqual(combined.sources.policy.materials, ('source1',))
                self.assertFalse(combined.sources.policy.require_design)  # Explicit caller choice remains authoritative.
                self.assertEqual(combined.sources.policy.require_summary, 'summary' in outputs)
                self.assertEqual(combined.state['config'].get('execution_contract') is not None,
                    'experiments' in outputs)
                self.assertEqual(combined.session.list_attempts(), ())
            with self.assertRaisesRegex(ValueError, 'execution.code_task'):
                create_research_task(ResearchBrief(request_text='Do not claim code work from a bare command.',
                    requested_outputs=('code_task', 'experiments')), root=base / 'invalid-code',
                    services=replace(services, config={'execution': {key: value for key, value in execution.items()
                        if key != 'code_task'}, 'require_design': False}))
            self.assertFalse((base / 'invalid-code').exists())

    def test_public_fixed_conditions_environment_and_confirmation_only_create(self):
        import sys
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            execution = {'command': [sys.executable, 'measure.py'], 'cwd': '.', 'timeout_sec': 5,
                'result_schema': {'primary_metric': 'score'}, 'baseline_policy': 'run',
                'seeds': [4, 9], 'seed_flag': '--seed',
                'environment': {'mode': 'current', 'timeout_sec': 5,
                    'check_command': [sys.executable, '-c', 'pass']}}
            original = copy.deepcopy(execution)
            for confirm in (False, True):
                services = ResearchApplicationServices(input_base_dir=base,
                    config={'execution': execution, 'execution_confirmation': confirm, 'require_design': False})
                app = create_research_task(ResearchBrief(request_text='Measure these exact conditions.',
                    requested_outputs=('experiments',)), root=base / str(confirm), services=services)
                saved = app.state['config']
                conditions = saved['execution_contract']['conditions']
                self.assertEqual([(row['role'], row['seed']) for row in conditions],
                    [('baseline', 4), ('candidate', 4), ('baseline', 9), ('candidate', 9)])
                self.assertTrue(all(row['execution']['cwd'] == str(base) for row in conditions))
                self.assertIsNone(saved.get('code_task'))
                self.assertEqual(app.ledger.remaining('process_invocations'), 5)
                self.assertEqual(app.ledger.remaining('process_wall_seconds'), 25)
                self.assertFalse(saved['allow_network'])
                if confirm:
                    self.assertTrue(saved['research_policy']['require_design'])
                    self.assertFalse(saved['research_policy']['allow_network'])
                    self.assertEqual(app.sources.pending_decision()['proposal_kind'], 'caller_execution_protocol')
                self.assertEqual(app.session.list_attempts(), ())
                self.assertFalse(app.ledger.entries)
            self.assertEqual(execution, original)

    def test_execution_mount_projection_preserves_explicit_source_aliases_and_suffixes(self):
        import sys
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            docs, project = base / 'docs', base / 'project'
            docs.mkdir()
            project.mkdir()
            (docs / 'paper.md').write_text('Declared method', encoding='utf-8')
            execution = {'command': [sys.executable, 'measure.py'], 'cwd': 'project', 'timeout_sec': 5,
                'result_schema': {'primary_metric': 'score'}, 'baseline_policy': 'skip'}
            for with_code in (False, True):
                declared = {**execution, **({'code_task': {'code_root': 'project',
                    'validation_command': ['python', 'check.py'], 'validation_timeout_sec': 5}} if with_code else {})}
                config = {'execution': declared, 'require_design': True, 'research_materials_only': True,
                    'materials': ['source0/paper.md'], 'source_workspace': 'source1' if with_code else 'source0'}
                original = copy.deepcopy(config)
                services = ResearchApplicationServices(llm_client=_JSONClient(), input_base_dir=base, config=config)
                brief = ResearchBrief(request_text='Honor declared materials.', requested_outputs=('experiments',),
                    asset_requests=({'locator': 'docs', 'role': 'material'},
                        *(({'locator': 'project', 'role': 'code'},) if with_code else ())))
                app = create_research_task(brief, root=base / str(with_code), services=services)
                policy = app.state['config']['research_policy']
                self.assertEqual(policy['materials'], ['source1/paper.md'] if with_code else ['source0/paper.md'])
                self.assertEqual(policy['source_workspace'], 'source0')
                if with_code:
                    self.assertEqual(app.state['config']['sources']['source1'], str(docs))
                    self.assertEqual(app.state['config']['sources']['source0'], str(project))
                else:
                    self.assertEqual(app.state['config']['sources']['source0'], str(docs))
                self.assertEqual(config, original)
                absolute = create_research_task(brief, root=base / f'absolute-{with_code}',
                    services=replace(services, config={**config, 'materials': [str(docs / 'paper.md')],
                        'source_workspace': str(project if with_code else docs)}))
                self.assertEqual(absolute.sources.policy.materials, (str(docs / 'paper.md'),))
                with patch.object(ToolApplication, 'create') as create, self.assertRaisesRegex(ValueError, 'original asset mount'):
                    create_research_task(brief, root=base / 'invalid', services=replace(services,
                        config={**config, 'materials': ['source9/paper.md']}))
                create.assert_not_called()
                if with_code:
                    with patch.object(ToolApplication, 'create') as create, self.assertRaisesRegex(ValueError, 'conflicts'):
                        create_research_task(brief, root=base / 'conflict', services=replace(services,
                            config={**config, 'source_workspace': 'source0'}))
                    create.assert_not_called()

    def test_public_execution_rejects_adoption_open_design_and_unknown_controls_before_creation(self):
        from simple_ar.app.research_intake import UnsupportedExecutionCreation
        import sys
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            execution = {'command': [sys.executable, 'measure.py'], 'cwd': str(base),
                'timeout_sec': 5, 'result_schema': {'primary_metric': 'score'}}
            cases = [
                ({**execution, 'code_task': {'run_dir': str(base / 'historical')}}, False),
                ({**execution, 'baseline_policy': 'reuse'}, False),
                ({**execution, 'unmapped_control': True}, False),
            ]
            with patch.object(ToolApplication, 'create') as create:
                for declared, design in cases:
                    with self.subTest(declared=declared), self.assertRaises(UnsupportedExecutionCreation):
                        create_research_task(ResearchBrief(request_text='Keep explicit ownership.',
                            requested_outputs=('experiments',)), root=base / 'task',
                            services=ResearchApplicationServices(input_base_dir=base,
                                config={'execution': declared, 'require_design': design}))
                create.assert_not_called()
            self.assertFalse((base / 'task').exists())

    def test_public_reuse_keeps_producing_lineage_with_original_owner(self):
        import sys
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            baseline = base / 'baseline.json'
            baseline.write_text(json.dumps({'implementation_ref': 'records/old/implementation.json',
                'preparation': {'attempt_id': 'old'}}), encoding='utf-8')
            original = baseline.read_bytes()
            with self.assertRaisesRegex(ValueError, 'baseline'):
                create_research_task(ResearchBrief(request_text='Reuse a measured baseline.',
                    requested_outputs=('experiments',)), root=base / 'task',
                    services=ResearchApplicationServices(input_base_dir=base,
                        config={'execution': {'command': [sys.executable, 'measure.py'], 'cwd': '.',
                            'timeout_sec': 5, 'result_schema': {'primary_metric': 'score'},
                            'baseline_policy': 'reuse', 'baseline_ref': 'baseline.json'},
                            'require_design': False}))
            self.assertEqual(baseline.read_bytes(), original)
            self.assertFalse((base / 'task').exists())

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
