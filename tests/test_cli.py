from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, create_autospec, patch

from rich.console import Console

from simple_ar.core.artifacts import read_json, write_json
from simple_ar.core import ArtifactStore
from simple_ar.cli.main import main
from simple_ar.cli.parser import build_parser
from simple_ar.cli.code_task_view import confirm_review_gate, render_execute_message
from simple_ar.core.reporting import style_progress_message


TEST_ROOT = Path(__file__).resolve().parents[1] / ".tmp_tests"


class CliTests(unittest.TestCase):
    def test_models_discovers_local_env_and_preserves_process_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            catalog = root / "models.toml"
            catalog.write_text('''version = 1
[profiles.daily]
api = "openai_chat"
base_url = "https://example.invalid/v1"
model = "local-test-model"
api_key_env = "LOCAL_TEST_KEY"
capabilities = ["text", "code"]
[routes]
default = "daily"
code = "daily"
''', encoding="utf-8")
            (root / ".env").write_text(
                f'SIMPLE_AR_MODELS_CONFIG="{catalog.as_posix()}"\nLOCAL_TEST_KEY=local-file-secret\n',
                encoding="utf-8")
            with patch.dict(os.environ, {"LOCAL_TEST_KEY": "process-secret"}, clear=True), \
                    patch("simple_ar.cli.main.Path.cwd", return_value=root), \
                    patch("simple_ar.cli.main.LLMClient.from_env") as client, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                main(["models", "--profile", "daily", "--purpose", "code"])
                self.assertIn("local-test-model", output.getvalue())
                self.assertIn("credential=set", output.getvalue())
                self.assertEqual(os.environ["LOCAL_TEST_KEY"], "process-secret")
                self.assertNotIn("process-secret", output.getvalue())
                self.assertNotIn("local-file-secret", output.getvalue())
                client.assert_not_called()

    def test_native_retry_model_cli_routes_only_explicit_selector_and_confirms_once(self):
        from simple_ar.core.budget import BudgetLedger

        app = MagicMock()
        app.state = {'config': {'model': 'profile:saved'}, 'pending': None}
        app.session.store.root = Path('saved-task').resolve()
        app.ledger = BudgetLedger({'llm_requests': 8, 'total_tokens': 100000})
        app.run.return_value = {'status': 'paused', 'status_reason': 'Saved response'}
        client = MagicMock()
        client.connection_binding.return_value = {'profile': 'new', 'settings': {
            'base_url': 'https://replacement.invalid/v1', 'model': 'replacement-model'}}
        base = ['start', '--resume-task', 'saved-task', '--retry-model']
        with patch('simple_ar.app.tool_application.ToolApplication.load', return_value=app), \
                patch('simple_ar.integrations.llm.LLMClient.from_env', return_value=client) as factory, \
                patch('simple_ar.cli.main._render_native_result'), \
                patch('sys.stdin.isatty', return_value=False), \
                patch.dict('os.environ', {'SIMPLE_AR_MODEL': 'profile:environment-default'}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            main(base)
            self.assertEqual(app.run.call_args.kwargs.get('retry_model_selector', ''), '')
            self.assertTrue(app.run.call_args.kwargs['retry_model'])
            factory.assert_not_called()
            app.run.reset_mock()
            for selector in ('profile:new', 'route:default'):
                with self.subTest(selector=selector):
                    main([*base, '--model', selector, '--prepare-only'])
                    app.run.assert_not_called()
                    main([*base, '--model', selector])
                    app.run.assert_not_called()
                    with patch('sys.stdin.isatty', return_value=True), \
                            patch('builtins.input', return_value='n') as confirm:
                        main([*base, '--model', selector])
                        confirm.assert_called_once()
                        app.run.assert_not_called()
                    with patch('sys.stdin.isatty', return_value=True), \
                            patch('builtins.input', return_value='y') as confirm:
                        main([*base, '--model', selector])
                        confirm.assert_called_once()
                        app.run.assert_called_once()
                    self.assertEqual(app.run.call_args.kwargs['retry_model_selector'], selector)
                    self.assertEqual(app.state['config']['model'], 'profile:saved')
                    app.run.reset_mock()
                    with patch('builtins.input') as confirm:
                        main([*base, '--model', selector, '--yes'])
                        confirm.assert_not_called()
                    app.run.assert_called_once()
                    self.assertEqual(app.run.call_args.kwargs['retry_model_selector'], selector)
                    app.run.reset_mock()
            self.assertIn('https://replacement.invalid/v1', output.getvalue())
            self.assertIn('replacement-model', output.getvalue())

    def test_native_resume_cli_rejects_explicit_model_change_without_retry(self):
        from simple_ar.core.budget import BudgetLedger

        app = MagicMock()
        app.state = {'config': {'model': 'profile:saved'}, 'pending': None}
        app.session.store.root = Path('saved-task').resolve()
        app.ledger = BudgetLedger({'llm_requests': 8, 'total_tokens': 100000})
        app.run.return_value = {'status': 'paused', 'status_reason': 'Saved response'}
        base = ['start', '--resume-task', 'saved-task']
        with patch('simple_ar.app.tool_application.ToolApplication.load', return_value=app), \
                patch('simple_ar.integrations.llm.LLMClient.from_env') as factory, \
                patch('simple_ar.cli.main._render_native_result'), \
                patch('sys.stdin.isatty', return_value=False), \
                patch.dict('os.environ', {'SIMPLE_AR_MODEL': 'profile:environment-default'}), \
                contextlib.redirect_stdout(io.StringIO()):
            before = app.ledger.to_dict()
            for selector in ('profile:new', 'route:default', 'raw-model'):
                with self.subTest(selector=selector), self.assertRaises(SystemExit):
                    main([*base, '--model', selector])
                app.run.assert_not_called()
                factory.assert_not_called()
                self.assertEqual(app.ledger.to_dict(), before)
                self.assertEqual(app.state['config']['model'], 'profile:saved')
            for flags in ([], ['--model', 'profile:saved']):
                main([*base, *flags])
                app.run.assert_called_once()
                self.assertFalse(app.run.call_args.kwargs['retry_model'])
                self.assertEqual(app.run.call_args.kwargs.get('retry_model_selector', ''), '')
                app.run.reset_mock()
            factory.assert_not_called()

    def test_visual_review_sends_snapshot_preserves_original_and_recovers_without_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "figure.png"
            image.write_bytes(b"rendered fixture")
            output = root / "review"
            client = MagicMock()
            client.connection_binding.return_value = {"profile": "visual"}
            client.ask_json.return_value = {"findings": [{"severity": "warning", "category": "layout",
                "summary": "Arrow crosses a node", "evidence": "source-0.png, right panel",
                "recommendation": "Route the return edge outside the node"}]}
            argv = ["image-review", "--input", str(image), "--goal", "Readable feedback loop",
                    "--output", str(output), "--model", "profile:visual"]
            with patch("simple_ar.integrations.llm.LLMClient.from_env", return_value=client) as factory, \
                    contextlib.redirect_stdout(io.StringIO()):
                main(argv)
                main(argv)
            self.assertEqual(factory.call_args.kwargs["purpose"], "vision")
            self.assertEqual(client.ask_json.call_count, 1)
            sent = client.ask_json.call_args.kwargs["image_paths"]
            self.assertEqual(sent, (output / "source-0.png",))
            self.assertEqual(sent[0].read_bytes(), image.read_bytes())
            self.assertIn("Arrow crosses", (output / "feedback.md").read_text())
            self.assertEqual(read_json(output / "review.json")["metadata"]["scientific_validity"], "not_assessed")
            self.assertEqual(read_json(output / "review.json")['findings'][0]['evidence'], ['source-0.png, right panel'])
            (output / 'review.json').unlink()
            with patch("simple_ar.integrations.llm.LLMClient.from_env", return_value=client), \
                    contextlib.redirect_stdout(io.StringIO()):
                main(argv)
            self.assertEqual(client.ask_json.call_count, 1)
            image.write_bytes(b"changed render")
            with patch("simple_ar.integrations.llm.LLMClient.from_env", return_value=client), \
                    self.assertRaisesRegex(SystemExit, "inputs changed"):
                main(argv)
            self.assertEqual(client.ask_json.call_count, 1)

    def test_writing_resume_keeps_explicit_paper_style_without_authorizing_execution(self):
        from simple_ar.cli.main import _report_config_overrides

        args = SimpleNamespace(command="research-session", report_template="experiment",
                               _explicit_resume_destinations={"report_template"})
        app = SimpleNamespace(services=SimpleNamespace(config={
            "research_task_kind": "writing", "report": {"template": "analysis_report"}}))
        self.assertEqual(_report_config_overrides(args, app), {"template": "experiment"})
        app.services.config["report"]["template"] = "experiment"
        self.assertEqual(_report_config_overrides(args, app), {})
        app.services.config["research_task_kind"] = "survey"
        self.assertEqual(_report_config_overrides(args, app), {"template": "survey"})

    def test_writing_cli_preserves_explicit_paper_template_and_material_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            material = Path(tmp) / "external notes.md"
            material.write_text("User notes", encoding="utf-8")
            app = MagicMock()
            view = SimpleNamespace(session_root=Path(tmp) / "session", status="completed", status_reason="",
                                   next_action=None, state_refs={}, attempts=())
            app.view.return_value = view
            app.advance.return_value = view
            app.services = SimpleNamespace(max_attempts=1)
            stream = io.StringIO()
            with patch("simple_ar.cli.main._optional_research_llm_client", return_value=object()), \
                 patch("simple_ar.app.research_application.create_session", return_value=app) as creator, \
                 contextlib.redirect_stdout(stream):
                main(["research-session", "--topic", "Write honestly", "--task-kind", "writing",
                      "--material", str(material), "--report-template", "experiment", "--output-root", str(Path(tmp) / "sessions")])
            brief = creator.call_args.args[0]
            services = creator.call_args.kwargs["services"]
            self.assertEqual(brief.requested_outputs, ("report",))
            self.assertEqual(brief.asset_requests[0]["role"], "material")
            self.assertEqual(services.config["report"]["template"], "experiment")
            self.assertEqual(services.config["report_writing_method"], "article")
            self.assertEqual(services.config["report"]["reviewer"], "disabled")
            self.assertFalse(services.config["report"]["document_review"])
            self.assertNotIn("execution", services.config)
            self.assertIn("Implementation: not requested (supplied-material writing)", stream.getvalue())
            from simple_ar.cli.research_config import report_writing_method
            selected = SimpleNamespace(_explicit_resume_destinations={"report_reviewer"},
                                       report_reviewer="llm", report_writing_method=None)
            self.assertEqual(report_writing_method(selected, {"reviewer": "llm"})[0], "sections")
            selected.report_writing_method = "article"
            with self.assertRaisesRegex(ValueError, "does not execute"):
                report_writing_method(selected, {"reviewer": "llm"})
            selected.command = "research-report"
            selected._explicit_resume_destinations = {"reviewer"}
            with self.assertRaisesRegex(ValueError, "does not execute"):
                report_writing_method(selected, {"reviewer": "llm"})
            article_app = SimpleNamespace(services=SimpleNamespace(config={
                "report_writing_method": "article", "report": {"reviewer": "disabled"}}))
            selected.reviewer = "llm"
            from simple_ar.cli.main import _report_config_overrides
            from simple_ar.app.research_application import ResearchApplicationError
            with self.assertRaises(ResearchApplicationError):
                _report_config_overrides(selected, article_app)
            bundle = Path(tmp) / "sources.json"
            write_json(bundle, {"schema_version": "document_bundle.v1", "documents": [{
                "document_id": "source", "source": "arxiv", "source_id": "1234.5678",
                "title": "Original source", "kind": "paper", "evidence_role": "original_source"}],
                "sections": [], "chunks": []})
            with patch("simple_ar.cli.main._optional_research_llm_client", return_value=object()), \
                 patch("simple_ar.app.research_application.create_session", return_value=app) as creator, \
                 contextlib.redirect_stdout(io.StringIO()):
                main(["research-session", "--topic", "Investigate a new question", "--task-kind", "survey",
                      "--material", str(bundle), "--output-root", str(Path(tmp) / "survey")])
            self.assertEqual(creator.call_args.args[0].asset_requests[0]["role"], "material")
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(SystemExit, "original-source"):
                main(["research-session", "--topic", "Do not treat notes as sources", "--task-kind", "survey",
                      "--material", str(material)])

    def test_code_task_init_same_second_reuses_unique_directory_owner(self):
        from datetime import datetime

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "module.py").write_text("value = 1\n", encoding="utf-8")
            task = root / "task.md"
            task.write_text("Inspect the module.\n", encoding="utf-8")
            outputs = root / "runs"
            args = ["code-task", "init", "--code-root", str(project), "--task-file", str(task),
                    "--workspace-mode", "copy", "--output-root", str(outputs), "--name", "same task"]
            with patch("simple_ar.app.session_roots.datetime") as clock, contextlib.redirect_stdout(io.StringIO()):
                clock.now.return_value = datetime(2026, 9, 30, 12, 0, 0)
                main(args)
                first = next(outputs.iterdir())
                original_manifest = (first / "manifest.json").read_bytes()
                main(args)
            runs = sorted(outputs.iterdir())
            self.assertEqual(len(runs), 2)
            self.assertEqual([path.name for path in runs],
                             ["20260930-120000-same-task", "20260930-120000-same-task-02"])
            self.assertEqual((first / "manifest.json").read_bytes(), original_manifest)
            self.assertTrue(all((path / "code_task/workspace/module.py").is_file() for path in runs))

    def test_fixed_protocol_does_not_claim_candidate_lineage_is_missing(self):
        from simple_ar.cli.research_view import method_validation_line
        for kind in ("measurement", "reproduction"):
            view = SimpleNamespace(work_plan={"task": {"kind": kind}})
            self.assertIsNone(method_validation_line(view))

    def test_unrun_matrix_is_not_displayed_as_an_experiment_artifact(self):
        from simple_ar.cli.research_view import _artifact_rows, method_validation_line

        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            root = Path(directory)
            store = ArtifactStore(root)
            pending = store.write_json("experiment-set.json", {
                "schema_version": "experiment_set.v1",
                "pairs": [{"seed": 0, "baseline": None, "candidate": None}],
            }, kind="experiment_set")
            view = SimpleNamespace(session_root=root, state_refs={"matrix_results": pending}, work_plan={})
            self.assertEqual(_artifact_rows(view), [])
            self.assertIsNone(method_validation_line(view))

    def test_feasibility_review_model_is_session_config_not_global_env(self):
        from simple_ar.cli.research_config import research_defaults

        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            path = Path(directory) / "research.toml"
            path.write_text('[task]\ngoal = "Review a method"\n[model]\nname = "author"\n'
                            'feasibility_review_model = "reviewer"\n', encoding="utf-8")
            defaults = research_defaults(["research-session", "--config", str(path)])
            self.assertEqual(defaults["feasibility_review_model"], "reviewer")
            args = build_parser(research_defaults=defaults).parse_args(
                ["research-session", "--config", str(path)])
            self.assertEqual(args.feasibility_review_model, "reviewer")

    def test_rich_completion_uses_measured_implementation_not_stale_initial_patch(self):
        from simple_ar.cli.research_view import ResearchConsole, method_validation_line

        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            root = Path(directory)
            store = ArtifactStore(root)
            initial = store.write_json("initial.json", {"method_validation": {"status": "未检查"}},
                                       kind="implementation_result")
            repaired = store.write_json("repaired.json", {"method_validation": {
                "status": "有执行证据支持", "reason": "A bounded observation was recorded.",
            }}, kind="implementation_result")
            old_result = store.write_json("old.json", {"implementation_ref": initial.to_dict()},
                                          kind="experiment_result")
            latest_result = store.write_json("latest.json", {"implementation_ref": repaired.to_dict()},
                                             kind="experiment_result")
            view = SimpleNamespace(
                work_plan={"accepted_plan": {"steps": [
                    {"capability": "experiment", "state_name": "experiment", "action": "experiment"},
                    {"capability": "experiment", "state_name": "experiment_repair_1", "action": "experiment_repair_1"},
                ]}},
                session_root=root, status="completed", next_action=None,
                status_reason="", attempts=(),
                state_refs={"experiment": old_result, "experiment_repair_1": latest_result},
            )
            self.assertEqual(method_validation_line(view), "Candidate method evidence: 有执行证据支持.")
            stream = io.StringIO()
            ResearchConsole(Console(file=stream, width=140)).finish(view)
            self.assertIn("Candidate method evidence: 有执行证据支持.", stream.getvalue())
            view.state_refs.pop("experiment_repair_1")
            self.assertEqual(method_validation_line(view), "Candidate method evidence: not independently checked.")

    def test_paired_completion_rejects_stale_or_mixed_method_lineage(self):
        from simple_ar.cli.research_view import method_validation_line

        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            root = Path(directory)
            store = ArtifactStore(root)
            initial = store.write_json("initial.json", {"method_validation": {"status": "未检查"}})
            repaired = store.write_json("repaired.json", {"method_validation": {"status": "checked"}})
            first = store.write_json("first.json", {"implementation_ref": repaired.to_dict()})
            second = store.write_json("second.json", {"implementation_ref": repaired.to_dict()})
            collection = store.write_json("collection.json", {
                "implementation_ref": initial.to_dict(),
                "pairs": [{"candidate": first.to_dict()}, {"candidate": second.to_dict()}],
            })
            view = SimpleNamespace(work_plan={}, session_root=root, state_refs={"matrix_results": collection})
            self.assertIn("collection lineage mismatch", method_validation_line(view))
            collection = store.write_json("collection-matched.json", {
                "implementation_ref": repaired.to_dict(),
                "pairs": [{"candidate": first.to_dict()}, {"candidate": second.to_dict()}],
            })
            view.state_refs["matrix_results"] = collection
            self.assertEqual(method_validation_line(view), "Candidate method evidence: checked.")
            other = store.write_json("other.json", {"implementation_ref": initial.to_dict()})
            mixed = store.write_json("collection-mixed.json", {
                "implementation_ref": repaired.to_dict(),
                "pairs": [{"candidate": first.to_dict()}, {"candidate": other.to_dict()}],
            })
            view.state_refs["matrix_results"] = mixed
            self.assertIn("mixed candidate implementations", method_validation_line(view))
            no_candidate = store.write_json("collection-baseline-only.json", {
                "implementation_ref": None, "pairs": [{"candidate": None}],
            })
            view.state_refs["matrix_results"] = no_candidate
            self.assertIsNone(method_validation_line(view))

    def test_rich_completion_distinguishes_session_and_report_audit_status(self):
        from simple_ar.cli.research_view import ResearchConsole, report_audit_line

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit_ref = ArtifactStore(root).write_json(
                "audit.json",
                {"status": "warning", "semantic_review_status": "semantic_unchecked"},
            )
            view = SimpleNamespace(
                work_plan={}, session_root=root, status="completed", next_action=None,
                status_reason="", attempts=(), state_refs={"report_audit": audit_ref},
            )
            stream = io.StringIO()
            ResearchConsole(Console(file=stream, width=120)).finish(view)
            self.assertIn("Report audit: warning", stream.getvalue())
            self.assertIn("semantic support is not certified", stream.getvalue())
            self.assertEqual(report_audit_line(view),
                             "Report audit: warning (semantic support is not certified).")

    def test_rich_failed_plan_shows_diagnostics_instead_of_empty_table(self):
        from simple_ar.cli.research_view import ResearchConsole
        stream = io.StringIO()
        renderer = ResearchConsole(Console(file=stream, width=160))
        view = SimpleNamespace(work_plan={}, session_root=Path("runs/failed-plan"),
                               status="paused", next_action="plan", status_reason="Invalid task plan",
                               attempts=(), state_refs={})
        with renderer.action("plan"):
            pass
        renderer.finish(view)
        output = stream.getvalue()
        self.assertIn("task, assets and constraints", output)
        self.assertIn("Attempt diagnostics:", output)
        self.assertNotIn("Location", output)

    def test_rich_displays_pending_decision_commands_without_mutating_state(self):
        from types import SimpleNamespace
        from simple_ar.cli.research_view import ResearchConsole

        view = SimpleNamespace(
            work_plan={"interaction": {"mode": "checkpoints", "decision": {
                "id": "0123456789abcdef", "stage": "execution_protocol", "status": "pending",
                "question": "Confirm the protocol.", "reason": "It sets the comparison.",
                "options": [{"id": "accept", "label": "Run the protocol"},
                            {"id": "revise", "label": "Change the protocol"},
                            {"id": "reject", "label": "Stop"}],
            }}, "task": {"goal": "A task with spaces"}},
            session_root=Path("C:/runs/a session"), status="paused", next_action=None,
            status_reason="Waiting for a decision.", attempts=(), state_refs={},
        )
        stream = io.StringIO()
        console = Console(file=stream, width=160)
        ResearchConsole(console).state(view)
        output = stream.getvalue()
        self.assertIn("Interaction: checkpoints", output)
        self.assertIn("0123456789abcdef", output)
        self.assertIn("simple-ar", output)
        self.assertIn("--session-root", output)
        self.assertIn("a session", output)
        self.assertIn("--decision-response", output)
        self.assertIn("REPLACE_WITH_YOUR_GUIDANCE", output)
        self.assertIn("Options: accept: Run the protocol", output)

        view.work_plan["interaction"]["decision"].update({
            "id": "deliverydecision01", "stage": "delivery", "status": "pending",
            "question": "Choose a report form.", "reason": "The result is inconclusive.",
            "options": [{"id": "revise", "label": "Choose a template"}],
        })
        ResearchConsole(console).state(view)
        delivery_output = stream.getvalue()
        self.assertIn("--report-template", delivery_output)
        self.assertIn("analysis_report", delivery_output)

    def test_delivery_revision_uses_the_pending_reply_not_request_report(self):
        from dataclasses import replace
        from simple_ar.app.research_application import (
            ResearchApplicationError, ResearchApplicationServices, create_session, load_session,
        )
        from simple_ar.research.workflow_contracts import ResearchBrief

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = root / "paper.md"
            paper.write_text("# Scope\nA supplied source for report delivery.", encoding="utf-8")
            session = root / "session"
            app = create_session(ResearchBrief(
                request_text="Summarize the supplied source.", objective="Summarize the supplied source.",
                requested_outputs=("report",),
                asset_requests=({"locator": str(paper), "role": "paper"},),
            ), root=session, services=ResearchApplicationServices(config={
                "interaction": "checkpoints", "research_materials_only": True,
                "report": {"mode": "research_only", "template": "auto"},
            }))
            for _ in range(16):
                if app.view().next_action == "report_write":
                    break
                app.advance()
            self.assertEqual(app.view().next_action, "report_write")
            decision_id = "abcdef0123456789"
            decision = {
                "action": "continue",
                "interaction": {
                    "id": decision_id, "stage": "delivery", "status": "pending",
                    "question": "Choose a report form.", "reason": "The requested delivery is unresolved.",
                    "options": [{"id": "accept"}, {"id": "revise"}, {"id": "reject"}],
                    "identity": app._interaction_identity(
                        "delivery", "report_write", delivery={"template": "analysis_report"},
                    ),
                },
            }
            proposal_ref = app.controller.store.write_json(
                f"outputs/research-decision-{decision_id}.json", decision,
                kind="research_decision", schema="research_decision.v1", producer="test",
            )
            app.controller.manifest.state_refs["decision"] = proposal_ref
            app.controller.save()
            app.controller.pause("Choose a report form.")
            base = ["research-session", "--session-root", str(session), "--topic", app.brief.objective]
            prompt = io.StringIO()
            with (
                patch("simple_ar.cli.main._optional_research_llm_client", return_value=None),
                patch("simple_ar.app.research_application.load_session", return_value=app),
                contextlib.redirect_stdout(prompt),
            ):
                with self.assertRaisesRegex(SystemExit, "status 'paused'"):
                    main(base)
            self.assertIn(decision_id, prompt.getvalue())
            self.assertIn("--report-template", prompt.getvalue())
            self.assertIn("analysis_report", prompt.getvalue())
            # Other resume branches must not silently discard a requested mode.
            for extra in (["--reanalyze"], ["--report-template", "analysis_report"]):
                with (
                    patch("simple_ar.cli.main._optional_research_llm_client", return_value=None),
                    patch("simple_ar.app.research_application.load_session", return_value=app),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    with self.assertRaisesRegex(SystemExit, "interaction mode change in a separate resume"):
                        main(base + ["--interaction", "autonomous"] + extra)
                self.assertEqual(app.services.config["interaction"], "checkpoints")
            argv = base + ["--interaction", "autonomous",
                           "--decision-id", decision_id, "--decision-response", "revise",
                           "--report-template", "analysis_report"]

            with (
                patch("simple_ar.cli.main._optional_research_llm_client", return_value=None),
                patch("simple_ar.app.research_application.load_session", return_value=app),
                patch.object(app.controller, "continue_with_revision", side_effect=RuntimeError("interrupted after reply")),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaisesRegex(SystemExit, "interrupted after reply"):
                    main(argv)

            response = app.controller.store.read_json(
                f"outputs/research-decision-{decision_id}-response.json",
            )
            self.assertEqual(response["interaction"]["response"]["revision"], {
                "report": {"template": "analysis_report"},
            })
            self.assertEqual(app.services.config["report"]["template"], "auto")
            app = load_session(session)
            with self.assertRaisesRegex(ResearchApplicationError, "different revision inputs"):
                app.continue_session(
                    decision_id=decision_id, decision_response="revise",
                    revised_report_config={"template": "survey"},
                )

            def stop_before_writer(**_kwargs):
                return replace(app.view(), status="paused", next_action=None,
                               status_reason="Test stops before report writing.")

            with (
                patch("simple_ar.cli.main._optional_research_llm_client", return_value=None),
                patch("simple_ar.app.research_application.load_session", return_value=app),
                patch.object(app, "advance", side_effect=stop_before_writer),
                patch.object(app, "request_report", wraps=app.request_report) as request_report,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaisesRegex(SystemExit, "status 'paused'"):
                    main(argv)
            request_report.assert_not_called()
            self.assertEqual(response["prior_decision_ref"], proposal_ref.to_dict())
            self.assertEqual(app.services.config["report"]["template"], "analysis_report")
            self.assertEqual(app.services.config["interaction"], "autonomous")
            restored = load_session(session)
            self.assertEqual(restored.services.config["report"]["template"], "analysis_report")
            self.assertEqual(restored.services.config["interaction"], "autonomous")

    def test_research_report_does_not_rewrite_or_execute_a_historical_session(self):
        from tests.legacy_session_fixture import historical_session
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "session"
            historical_session(root)
            before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            with patch("simple_ar.cli.main._optional_research_llm_client", return_value=object()):
                with self.assertRaisesRegex(SystemExit, "No legacy workflow was executed"):
                    main(["research-report", "--session-root", str(root), "--model", "scripted"])
            after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            self.assertEqual(before, after)

    def test_segmented_code_task_report_retires_before_loading_config(self):
        with self.assertRaisesRegex(SystemExit, "research-session"):
            main(["research-code-task", "--topic", "task", "--synthesis-file", "missing.json",
                  "--code-task-config", "missing.toml", "--with-report"])

    def test_historical_stage_status_is_readable_without_running_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = {"topic": "Historical research", "stages": [
                {"stage_number": 1, "stage": "plan", "status": "done", "outputs": ["problem.md"]}
            ]}
            write_json(root / "manifest.json", manifest)
            write_json(root / "pipeline_state.json", {
                "status": "failed", "last_stage": "search", "next_stage": "read",
            })
            write_json(root / "state.json", {
                "schema_version": "workspace_state.v1", "run_id": "archived",
                "topic": "Historical research", "search": {"status": "failed"},
                "artifact_aliases": {"papers.jsonl": "02-search/papers.jsonl"},
            })
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                main(["status", str(root)])
            self.assertIn("Historical research", output.getvalue())
            self.assertIn("01 plan:", output.getvalue())
            self.assertIn("Pipeline: failed (last=search, next=read)", output.getvalue())
            self.assertEqual(read_json(root / "manifest.json"), manifest)
            self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before)

    def test_retired_pipeline_commands_do_not_read_config_or_write_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for command in ("run", "resume", "research-code-task", "research-experiment"):
                with self.subTest(command=command), self.assertRaisesRegex(SystemExit, "retired.*research-session"):
                    main([command, "--config", str(root / "missing.toml"),
                          "--output-root", str(root / "runs")])
            self.assertEqual(list(root.iterdir()), [])

    def test_research_session_parser_allows_literature_only_mode(self) -> None:
        args = build_parser().parse_args(
            ["research-session", "--topic", "reliable agents", "--no-report"]
        )

        self.assertIsNone(args.command_argv)
        self.assertIsNone(args.code_task_config)

    def test_research_session_interrupted_recovery_requires_saved_session(self) -> None:
        args = build_parser(allow_resume_without_topic=True).parse_args([
            "research-session", "--session-root", "saved", "--recover-interrupted",
        ])
        self.assertTrue(args.recover_interrupted)
        with self.assertRaisesRegex(SystemExit, "requires --session-root"):
            main(["research-session", "--topic", "test", "--recover-interrupted"])

    def test_research_session_cli_recovers_stopped_worker_and_resumes_saved_plan(self) -> None:
        from simple_ar.app.research_application import (
            ResearchApplicationServices, create_session, load_session,
        )
        from simple_ar.research.workflow_contracts import ResearchBrief

        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            paper = root / "paper.md"
            paper.write_text("# Evidence\nThis source contains a bounded claim.\n", encoding="utf-8")
            session = root / "session"
            app = create_session(ResearchBrief(
                request_text="Summarize the supplied paper.",
                objective="Summarize the supplied paper.",
                requested_outputs=("summary",),
                asset_requests=({"locator": str(paper), "role": "paper"},),
            ), root=session, services=ResearchApplicationServices(
                config={"research_materials_only": True},
            ))

            def interrupt(**_):
                raise KeyboardInterrupt()

            app.controller.registry.register("plan", interrupt, replace=True)
            with self.assertRaises(KeyboardInterrupt):
                app.advance()
            with contextlib.redirect_stdout(io.StringIO()):
                main(["research-session", "--session-root", str(session),
                      "--recover-interrupted", "--no-report"])
            resumed = load_session(session)
            self.assertEqual(
                sum(row["status"] == "failed" for row in resumed.view().attempts), 1,
            )
            self.assertIn("task_plan", resumed.view().state_refs)
            self.assertEqual(resumed.view().budget["attempts"], len(resumed.view().attempts))
            # Historical CodeTask continues through its saved orchestrator,
            # including greenfield strategy; it is not converted to native.
            saved = root / "saved-code-task.toml"
            saved.write_text('[execute]\nmodel="profile:saved"\nplanning_mode="compact"\n'
                             'repair_rounds=2\n[environment]\nmode="current"\n', encoding="utf-8")
            code_run = root / "old-code-run"
            before = saved.read_bytes()
            with patch("simple_ar.cli.main.execute_code_task") as execute, \
                    patch("simple_ar.cli.main.render_execute_header"), \
                    patch("simple_ar.cli.main.render_execute_result"), \
                    patch("simple_ar.app.tool_application.ToolApplication.create") as native:
                main(["code-task", "execute", str(code_run), "--config", str(saved),
                      "--to-step", "validate", "--no-llm", "--no-review-inline"])
            execute.assert_called_once()
            self.assertEqual(execute.call_args.args[0], code_run)
            self.assertEqual(execute.call_args.kwargs["to_step"], "validate")
            self.assertEqual(execute.call_args.kwargs["planning_mode"], "compact")
            self.assertEqual(execute.call_args.kwargs["repair_rounds"], 2)
            self.assertEqual(execute.call_args.kwargs["model"], "profile:saved")
            self.assertFalse(execute.call_args.kwargs["use_llm"])
            self.assertEqual(saved.read_bytes(), before)
            native.assert_not_called()

    def test_research_session_cli_resume_without_model_fails_before_mutation(self) -> None:
        from simple_ar.app.research_application import (
            ResearchApplicationServices, create_session,
        )
        from simple_ar.research.workflow_contracts import ResearchBrief

        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "session"
            create_session(ResearchBrief(
                request_text="Summarize this question.",
                objective="Summarize this question.",
                requested_outputs=("summary",),
            ), root=session, services=ResearchApplicationServices(llm_client=object()))
            before = {path.relative_to(session): path.read_bytes()
                      for path in session.rglob("*") if path.is_file()}
            with self.assertRaisesRegex(SystemExit, "--model env or --model MODEL"):
                main(["research-session", "--session-root", str(session)])
            after = {path.relative_to(session): path.read_bytes()
                     for path in session.rglob("*") if path.is_file()}
            self.assertEqual(after, before)

    def test_research_session_cli_without_command_stays_literature_only(self) -> None:
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            session_root = root / "sessions" / "research-session"
            final_view = SimpleNamespace(
                session_root=session_root,
                status="completed",
                status_reason="",
                next_action=None,
                state_refs={"summary": SimpleNamespace(path="outputs/research_summary.md")},
                attempts=(),
            )
            app = MagicMock()
            app.services = SimpleNamespace(max_attempts=5)
            app.view.side_effect = [
                SimpleNamespace(
                    session_root=session_root, status="running", status_reason="",
                    next_action="plan", state_refs={}, attempts=(),
                ),
                final_view,
            ]
            app.advance.return_value = final_view
            stdout = io.StringIO()
            with (
                patch("simple_ar.cli.main._optional_research_llm_client", return_value=None),
                patch("simple_ar.app.research_application.create_session", return_value=app) as creator,
                contextlib.redirect_stdout(stdout),
            ):
                main([
                    "research-session",
                    "--topic", "reliable agents",
                    "--output-root", str(root / "sessions"),
                    "--no-report",
                ])

            brief = creator.call_args.args[0]
            services = creator.call_args.kwargs["services"]
            self.assertEqual(brief.requested_outputs, ("summary",))
            self.assertEqual(brief.intents, ("research",))
            self.assertNotIn("execution", services.config)
            self.assertEqual(services.budget_limits["process_invocations"], 0)
            self.assertIn("summary: ", stdout.getvalue())

    def test_research_session_cli_without_command_with_model_requests_survey_report(self) -> None:
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            session_root = root / "sessions" / "research-session"
            final_view = SimpleNamespace(
                session_root=session_root,
                status="completed",
                status_reason="",
                next_action=None,
                state_refs={"report": SimpleNamespace(path="attempts/report/report.md")},
                attempts=(),
            )
            app = MagicMock()
            app.services = SimpleNamespace(max_attempts=5)
            app.view.side_effect = [
                SimpleNamespace(
                    session_root=session_root, status="running", status_reason="",
                    next_action="plan", state_refs={}, attempts=(),
                ),
                final_view,
            ]
            app.advance.return_value = final_view
            with (
                patch("simple_ar.cli.main._optional_research_llm_client", return_value=object()),
                patch("simple_ar.app.research_application.create_session", return_value=app) as creator,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                main([
                    "research-session",
                    "--topic", "reliable agents",
                    "--output-root", str(root / "sessions"),
                ])

            brief = creator.call_args.args[0]
            services = creator.call_args.kwargs["services"]
            self.assertEqual(brief.requested_outputs, ("report",))
            self.assertEqual(services.config["report"]["mode"], "research_only")
            self.assertEqual(services.config["report"]["template"], "auto")
            self.assertEqual(services.budget_limits["process_wall_seconds"], 0)

    def test_research_session_parser_accepts_shared_cache_dir(self) -> None:
        args = build_parser().parse_args(
            [
                "research-session",
                "--topic",
                "reliable agents",
                "--cache-dir",
                "shared-cache",
                "--no-report",
                "--command",
                sys.executable,
                "-c",
                "print('accuracy: 0.9')",
            ]
        )

        self.assertEqual(args.cache_dir, "shared-cache")
        self.assertTrue(args.no_report)
        self.assertEqual(args.command, "research-session")

    def test_research_session_continue_parser_keeps_revised_command(self) -> None:
        args = build_parser().parse_args(
            [
                "research-session-continue",
                "--session-root",
                "runs/session",
                "--primary-metric",
                "accuracy",
                "--metric-direction",
                "accuracy=higher",
                "--command",
                sys.executable,
                "-c",
                "print('accuracy: 0.9')",
            ]
        )

        self.assertEqual(args.command, "research-session-continue")
        self.assertIsNone(args.parent_attempt_id)
        self.assertEqual(
            args.command_argv,
            [sys.executable, "-c", "print('accuracy: 0.9')"],
        )

    def test_canonical_recovery_cli_uses_application_retry_boundary(self) -> None:
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp) / "session"
            root.mkdir()
            (root / "session_manifest.json").write_text("{}", encoding="utf-8")
            view = SimpleNamespace(
                session_root=root,
                status="completed",
                next_action=None,
                state_refs={
                    "experiment": SimpleNamespace(
                        path="attempts/experiment-0002/experiment_result.json"
                    )
                },
            )
            app = MagicMock()
            app.retry_experiment.return_value = view
            stdout = io.StringIO()
            with patch(
                "simple_ar.app.research_application.load_session", return_value=app
            ), contextlib.redirect_stdout(stdout):
                main([
                    "research-session-continue",
                    "--session-root",
                    str(root),
                    "--cwd",
                    str(root),
                    "--command",
                    sys.executable,
                    "-c",
                    "print('accuracy: 0.9')",
                ])

            call = app.retry_experiment.call_args
            self.assertEqual(call.kwargs["command"], (sys.executable, "-c", "print('accuracy: 0.9')"))
            self.assertIsNone(call.kwargs["parent_attempt_id"])
            self.assertIn("Status: completed", stdout.getvalue())
            self.assertIn("experiment-0002", stdout.getvalue())

    def test_legacy_session_continue_does_not_execute_or_rewrite_history(self) -> None:
        from tests.legacy_session_fixture import historical_session

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "legacy"
            historical_session(root, failed=True)
            before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            with self.assertRaises(SystemExit):
                main([
                    "research-session-continue", "--session-root", str(root),
                    "--cwd", str(root), "--command", sys.executable, "-c",
                    "from pathlib import Path; Path('unexpected-execution').touch()",
                ])
            after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            self.assertEqual(after, before)

    def test_research_session_migrate_parser_accepts_explicit_artifacts(self) -> None:
        args = build_parser().parse_args(
            [
                "research-session-migrate",
                "--source-root",
                "runs/legacy",
                "--destination-root",
                "runs/successor",
                "--artifact",
                "search",
                "--requested-output",
                "report",
            ]
        )

        self.assertEqual(args.command, "research-session-migrate")
        self.assertEqual(args.artifact_names, ["search"])
        self.assertEqual(args.requested_outputs, ["report"])

    def test_research_session_migrate_cli_reports_new_successor(self) -> None:
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            successor = root / "successor"
            store = ArtifactStore(legacy)
            search_ref = store.write_json(
                "state/search.json",
                {"legacy": True},
                kind="search",
                schema="legacy_search.v1",
                producer="legacy",
            )
            store.write_json(
                "session_manifest.json",
                {
                    "schema_version": "session_manifest.v1",
                    "session_id": "legacy-cli-session",
                    "topic": "CLI migration",
                    "status": "created",
                    "state_refs": {"search": search_ref.to_dict()},
                    "budget": {},
                    "decisions": [],
                },
                kind="session",
                schema="session_manifest.v1",
                producer="legacy",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                main(
                    [
                        "research-session-migrate",
                        "--source-root",
                        str(legacy),
                        "--destination-root",
                        str(successor),
                        "--artifact",
                        "search",
                    ]
                )

            self.assertIn("Successor session:", stdout.getvalue())
            self.assertIn("Historical budget: unknown_not_imported", stdout.getvalue())
            manifest = ArtifactStore(successor).read_json("session_manifest.json")
            self.assertEqual(manifest["parent_session"], "legacy-cli-session")
            self.assertTrue(
                (successor / "compatibility" / "imported" / "01-search.json").is_file()
            )

    def test_code_task_execute_messages_use_shared_rich_styles(self) -> None:
        self.assertEqual(style_progress_message("LLM usage greenfield-file-main.py: 1 input"), "gold1")
        self.assertEqual(
            style_progress_message("Dependency advice: missing optional packages: torch."),
            "bright_yellow",
        )

        stream = io.StringIO()
        console = Console(
            file=stream,
            force_terminal=True,
            color_system="standard",
            highlight=False,
            soft_wrap=True,
            emoji=False,
        )
        render_execute_message("LLM usage greenfield-file-main.py: 1 input", console=console)

        output = stream.getvalue()
        self.assertIn("LLM usage greenfield-file-main.py", output)
        self.assertIn("\x1b[", output)

    def test_review_gate_without_input_stops_cleanly(self) -> None:
        class FakeConsole:
            def __init__(self) -> None:
                self.messages: list[str] = []

            def input(self, _prompt: str) -> str:
                raise EOFError

            def print(self, message: object) -> None:
                self.messages.append(str(message))

        console = FakeConsole()

        self.assertFalse(confirm_review_gate("Approve?", console=console))
        self.assertTrue(confirm_review_gate("Approve?", console=console, assume_yes=True))
        self.assertTrue(any("No interactive input" in message for message in console.messages))


    def test_status_reports_persisted_research_session_checkpoint(self) -> None:
        from simple_ar.core.capabilities import CapabilityRegistry
        from simple_ar.core.session import SessionController

        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            session_root = Path(tmp) / "session"
            SessionController.create(
                session_root,
                session_id="session-status-test",
                topic="checkpoint topic",
                profile="research_brief",
                registry=CapabilityRegistry(),
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                main(["status", str(session_root)])

            status_text = stdout.getvalue()
            self.assertIn("Session: ", status_text)
            self.assertIn("Topic: checkpoint topic", status_text)
            self.assertIn("Profile: research_brief", status_text)
            self.assertIn("Status: created", status_text)
            self.assertIn("Attempts:\n- none", status_text)

    def test_status_reports_ready_for_report_handoff(self) -> None:
        from simple_ar.core import ArtifactStore

        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            session_root = Path(tmp) / "session"
            # Recorded legacy shape: status inspection must not require a
            # functioning old workflow executor to reconstruct history.
            ArtifactStore(session_root).write_json("session_manifest.json", {
                "schema_version": "session_manifest.v1",
                "session_id": "historical-status",
                "topic": "historical handoff",
                "status": "running",
                "budget": {"attempts": 1},
                "decisions": [{
                    "capability": "analysis", "attempt_id": "analysis-001",
                    "action": "accept", "result_status": "completed",
                    "next_capability": "report",
                }],
            })
            original = (session_root / "session_manifest.json").read_bytes()

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                main(["status", str(session_root)])
            self.assertEqual((session_root / "session_manifest.json").read_bytes(), original)

            self.assertIn("Handoff: ready_for_report (next=report)", stdout.getvalue())

    def test_status_reports_explicit_failure_continuation(self) -> None:
        from simple_ar.core import ArtifactStore

        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            session_root = Path(tmp) / "session"
            # Recorded legacy shape: status inspection must not require a
            # functioning old workflow executor to reconstruct history.
            ArtifactStore(session_root).write_json("session_manifest.json", {
                "schema_version": "session_manifest.v1",
                "session_id": "historical-status",
                "topic": "historical handoff",
                "status": "running",
                "budget": {"attempts": 1},
                "decisions": [{
                    "capability": "analysis", "attempt_id": "analysis-001",
                    "action": "repair", "result_status": "failed",
                    "next_capability": "analysis",
                }],
            })
            original = (session_root / "session_manifest.json").read_bytes()

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                main(["status", str(session_root)])
            self.assertEqual((session_root / "session_manifest.json").read_bytes(), original)

            status_text = stdout.getvalue()
            self.assertIn("Status: running", status_text)
            self.assertIn("Continuation: explicit repair -> analysis", status_text)
            self.assertIn("0 running", status_text)


    def test_research_report_cli_reads_existing_session_without_rerunning_it(self) -> None:
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            output_root = root / "sessions"
            session_root = output_root / "existing-session"
            session_root.mkdir(parents=True)
            final_view = SimpleNamespace(
                session_root=session_root,
                status="completed",
                status_reason="",
                next_action=None,
                state_refs={
                    "report": SimpleNamespace(path="attempts/report-001/report.md"),
                },
                attempts=(),
            )
            app = MagicMock()
            app.brief = SimpleNamespace(requested_outputs=("experiments",))
            app.services = SimpleNamespace(max_attempts=4, config={})
            app.controller = SimpleNamespace(
                manifest=SimpleNamespace(budget=SimpleNamespace(max_attempts=4)),
            )
            app.view.return_value = SimpleNamespace(
                session_root=session_root,
                status="completed",
                status_reason="",
                next_action=None,
                state_refs={},
                attempts=(),
            )
            app.request_report.return_value = SimpleNamespace(
                session_root=session_root,
                status="running",
                status_reason="",
                next_action=None,
                state_refs={},
                attempts=(),
            )
            app.advance.return_value = final_view
            stdout = io.StringIO()
            with (
                patch("simple_ar.cli.main._optional_research_llm_client", return_value=object()),
                patch(
                    "simple_ar.app.research_application.load_session",
                    return_value=app,
                ) as loader,
                patch("simple_ar.app.research_application.create_session", side_effect=AssertionError("must not create a new session")),
                contextlib.redirect_stdout(stdout),
            ):
                main(
                    [
                        "research-report",
                        "--session-root",
                        str(session_root),
                        "--model",
                        "gpt-5.4",
                        "--template",
                        "survey",
                    ]
                )

            loader.assert_called_once()
            app.request_report.assert_called_once_with(
                report_config={"template": "survey"},
                reason="Apply explicit report settings and rebuild only report deliverables from existing evidence.",
            )
            app.advance.assert_called_once_with(max_actions=1)
            self.assertIn("Status: completed", stdout.getvalue())
            self.assertIn(
                str(session_root / "attempts" / "report-001" / "report.md"),
                stdout.getvalue(),
            )

    def test_report_resume_uses_real_interface_and_persisted_attempt_limit(self):
        from simple_ar.app.research_application import ResearchApplication

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "session"
            app = create_autospec(ResearchApplication, instance=True)
            app.brief = SimpleNamespace(objective="A bounded report", requested_outputs=("report",))
            app.services = SimpleNamespace(
                max_attempts=1, llm_client=SimpleNamespace(model="scripted"), config={},
            )
            app.controller = SimpleNamespace(
                manifest=SimpleNamespace(budget=SimpleNamespace(max_attempts=3)),
            )
            paused = SimpleNamespace(
                session_root=root, status="paused", status_reason="retry report",
                next_action="report_write", state_refs={},
            )
            running = SimpleNamespace(
                session_root=root, status="running", status_reason="",
                next_action="report_write", state_refs={},
            )
            completed = SimpleNamespace(
                session_root=root, status="completed", status_reason="",
                next_action=None,
                state_refs={"report": SimpleNamespace(path="attempts/report-001/report.md")},
            )
            app.view.return_value = paused
            app.continue_session.return_value = running
            app.advance.side_effect = [running] * 9 + [completed]
            with (
                patch("simple_ar.cli.main._optional_research_llm_client", return_value=object()),
                patch("simple_ar.app.research_application.load_session", return_value=app),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                main(["research-report", "--session-root", str(root), "--model", "scripted"])
            app.continue_session.assert_called_once_with(
                reason="Resume the canonical report lifecycle within the persisted session budget.",
            )
            self.assertEqual(app.advance.call_count, 10)

    def test_exhausted_report_resume_shows_bounded_authorization_entrypoint(self):
        from simple_ar.app.research_application import ResearchApplication

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "session"
            app = create_autospec(ResearchApplication, instance=True)
            app.brief = SimpleNamespace(objective="A bounded report", requested_outputs=("report",))
            app.services = SimpleNamespace(config={}, llm_client=SimpleNamespace(model="scripted"))
            app.controller = SimpleNamespace(
                manifest=SimpleNamespace(
                    budget=SimpleNamespace(
                        attempts=5, max_attempts=5, no_progress=3, max_no_progress=3,
                    ),
                    session_id="session-123", revision=7,
                ),
                store=SimpleNamespace(root=root),
            )
            app.budget_ledger = SimpleNamespace(limits={}, remaining=lambda _name: None)
            app.view.return_value = SimpleNamespace(
                session_root=root, status="paused", next_action="report_write", state_refs={},
            )
            app.continue_session.side_effect = RuntimeError("Session budget is exhausted.")
            output = io.StringIO()
            with (
                patch("simple_ar.cli.main._optional_research_llm_client", return_value=object()),
                patch("simple_ar.app.research_application.load_session", return_value=app),
                contextlib.redirect_stdout(output),
            ):
                with self.assertRaisesRegex(
                    SystemExit, "--additional-attempts 3 --additional-no-progress 1",
                ) as raised:
                    main(["research-report", "--session-root", str(root), "--model", "scripted"])
            self.assertIn("simple-ar research-session", str(raised.exception))
            self.assertIn("--topic 'A bounded report'", str(raised.exception))
            self.assertNotIn("allow_no_progress_exhausted", str(raised.exception))

    def test_completed_report_authorization_refresh_and_recovery_without_processes(self):
        from dataclasses import replace
        from simple_ar.app.research_application import create_session, load_session, ResearchApplicationServices
        from simple_ar.research.workflow_contracts import ResearchBrief
        from simple_ar.report.schema import AgentReportResult, ReportSectionDraft
        from simple_ar.research.task_plan import TaskPlanRequest, default_task_steps

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = root / "paper.md"
            paper.write_text("# Calibration\nCalibration evidence is limited.\n", encoding="utf-8")
            session = root / "session"
            brief = ResearchBrief(
                request_text="Review calibration.", objective="Review calibration.",
                requested_outputs=("report",),
                asset_requests=({"locator": str(paper), "role": "paper"},),
            )
            planned_actions = [row["action"] for row in default_task_steps(TaskPlanRequest(
                task_kind="survey", goal=brief.objective, request_text=brief.request_text,
                requested_outputs=brief.requested_outputs,
                config={"research_local_documents": [str(paper)]}))]
            initial_attempt_limit = 1 + len(planned_actions)  # One plan attempt plus its actual route.
            refreshed_attempt_limit = initial_attempt_limit + 4
            app = create_session(brief, root=session, services=ResearchApplicationServices(
                max_results=1, max_attempts=initial_attempt_limit,
                budget_limits={"llm_requests": 10, "total_tokens": 100,
                               "process_invocations": 0, "process_wall_seconds": 0},
            ))
            for _ in range(initial_attempt_limit):
                if app.view().next_action == "report_write":
                    break
                prepared = app.advance(max_actions=1)
                self.assertNotIn(prepared.status, {"paused", "blocked", "completed"}, prepared.status_reason)
            self.assertEqual(app.view().next_action, "report_write", app.view().status_reason)
            self.assertFalse(any(attempt.capability == "report_write" for attempt in app.controller.list_attempts()))
            accepted_actions = [step.action for step in app._load_task_plan().steps]
            self.assertEqual(accepted_actions, planned_actions)
            remaining_actions = accepted_actions[accepted_actions.index("report_write"):]
            self.assertEqual(remaining_actions, ["report_write", "report", "report_audit"])
            app.services = replace(app.services, llm_client=object())

            def writer(**kwargs):
                paper_id = kwargs["context"].papers[0]["id"]
                return AgentReportResult(report_body="", memory=kwargs["memory"], used_agent=True,
                    sections=[ReportSectionDraft(section_id="review", heading="Evidence",
                        draft_markdown=f"Calibration evidence is limited [@{paper_id}].",
                        used_sources=[paper_id])])

            with patch("simple_ar.report.writing.run_report_agent", side_effect=writer):
                initial = app.advance(max_actions=len(remaining_actions))
            self.assertEqual(initial.status, "completed", app.controller.manifest.status_reason)
            self.assertEqual(initial.budget["attempts"], initial_attempt_limit)
            self.assertTrue(app.controller.manifest.budget.exhausted())
            old_report = session / initial.state_refs["report"].path
            old_body = old_report.read_bytes()
            resume = ["research-session", "--session-root", str(session), "--topic", "Review calibration."]
            authorize = resume + ["--authorization-id", "report-refresh-1",
                "--authorization-reason", "One report refresh and one failed attempt.", "--additional-attempts", "4"]
            report = ["research-report", "--session-root", str(session), "--model", "scripted"]
            with patch("simple_ar.cli.main._optional_research_llm_client", return_value=object()), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main(report + ["--refresh"])
                self.assertIn("--additional-attempts 3", str(raised.exception))
                self.assertNotIn("process_invocations=", str(raised.exception))
                self.assertNotIn("process_wall_seconds=", str(raised.exception))
                self.assertIn("then repeat", str(raised.exception))
                self.assertEqual(load_session(session).view().state_refs, initial.state_refs)

                resources = resume + ["--authorization-id", "report-resources-1",
                    "--authorization-reason", "Replenish tokens before authorizing attempts.",
                    "--authorize-remaining", "total_tokens=200"]
                main(resources)  # Authorization need not enable execution immediately.
                main(resources)
                still_completed = load_session(session)
                self.assertEqual(still_completed.view().attempts, initial.attempts)
                self.assertTrue(still_completed.controller.manifest.budget.exhausted())
                self.assertEqual(still_completed.budget_ledger.limits["total_tokens"], 200)

                for _ in range(2):
                    main(authorize)
                    view = load_session(session).view()
                    self.assertEqual((view.status, view.revision, view.attempts, view.state_refs),
                                     (initial.status, initial.revision, initial.attempts, initial.state_refs))
                    self.assertEqual(view.budget["max_attempts"], refreshed_attempt_limit)
                with self.assertRaisesRegex(SystemExit, "different terms"):
                    main(authorize[:-1] + ["5"])

                with patch("simple_ar.report.writing.run_report_agent", side_effect=RuntimeError("fixture interruption")):
                    with self.assertRaises(SystemExit):
                        main(report + ["--refresh"])
                self.assertEqual(load_session(session).view().status, "paused")
                with patch("simple_ar.report.writing.run_report_agent", side_effect=writer):
                    main(report)
                completed = load_session(session).view()
                self.assertEqual(completed.status, "completed")
                self.assertEqual(completed.budget["attempts"], refreshed_attempt_limit)
                self.assertNotEqual(completed.state_refs["report"], initial.state_refs["report"])
                main(authorize)  # Replay remains safe even after all four attempts were spent.
                main(report)
            restored = load_session(session)
            self.assertEqual(restored.view().attempts, completed.attempts)
            self.assertEqual(restored.view().state_refs["read"], initial.state_refs["read"])
            self.assertEqual(old_report.read_bytes(), old_body)
            self.assertEqual(restored.budget_ledger.limits["process_invocations"], 0)
            self.assertEqual(restored.budget_ledger.limits["process_wall_seconds"], 0)
            self.assertFalse(any(row["capability"] == "experiment" for row in completed.attempts))

    def test_research_session_cli_can_append_report_in_one_explicit_flow(self) -> None:
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            paper = root / "reliable_agents.md"
            paper.write_text(
                "# Results\n\nThe fixture reports accuracy: 0.75.\n",
                encoding="utf-8",
            )
            output_root = root / "sessions"
            session_root = output_root / "research-session"
            final_view = SimpleNamespace(
                session_root=session_root,
                status="completed",
                status_reason="",
                next_action=None,
                state_refs={
                    "report": SimpleNamespace(path="attempts/report-001/report.md"),
                },
                attempts=(),
            )
            app = MagicMock()
            app.services = SimpleNamespace(max_attempts=5)
            app.view.side_effect = [
                SimpleNamespace(
                    session_root=session_root, status="running", status_reason="",
                    next_action="plan", state_refs={}, attempts=(),
                ),
                final_view,
            ]
            app.advance.return_value = final_view
            with (
                patch("simple_ar.cli.main._optional_research_llm_client", return_value=object()),
                patch(
                    "simple_ar.app.research_application.create_session",
                    return_value=app,
                ) as creator,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                main(
                    [
                        "research-session",
                        "--topic",
                        "reliable agents",
                        "--local-document",
                        str(paper),
                        "--output-root",
                        str(output_root),
                        "--cwd",
                        str(root),
                        "--model",
                        "gpt-5.4",
                        "--with-report",
                        "--report-reviewer",
                        "disabled",
                        "--max-review-iterations",
                        "0",
                        "--command",
                        sys.executable,
                        "-c",
                        "print('accuracy: 0.75')",
                    ]
                )

            creator.assert_called_once()
            self.assertEqual(creator.call_args.args[0].requested_outputs, ("experiments", "report"))
            app.advance.assert_called_once_with(max_actions=1)

    def test_research_session_cli_defaults_to_report_with_model(self) -> None:
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            paper = root / "reliable_agents.md"
            paper.write_text(
                "# Results\n\nThe fixture reports accuracy: 0.75.\n",
                encoding="utf-8",
            )
            output_root = root / "sessions"
            session_root = output_root / "research-session"
            final_view = SimpleNamespace(
                session_root=session_root,
                status="completed",
                status_reason="",
                next_action=None,
                state_refs={
                    "report": SimpleNamespace(path="attempts/report-001/report.md"),
                },
                attempts=(),
            )
            app = MagicMock()
            app.services = SimpleNamespace(max_attempts=5)
            app.view.side_effect = [
                SimpleNamespace(
                    session_root=session_root, status="running", status_reason="",
                    next_action="plan", state_refs={}, attempts=(),
                ),
                final_view,
            ]
            app.advance.return_value = final_view
            with (
                patch("simple_ar.cli.main._optional_research_llm_client", return_value=object()),
                patch(
                    "simple_ar.app.research_application.create_session",
                    return_value=app,
                ) as creator,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                main(
                    [
                        "research-session",
                        "--topic",
                        "reliable agents",
                        "--local-document",
                        str(paper),
                        "--output-root",
                        str(output_root),
                        "--cwd",
                        str(root),
                        "--model",
                        "gpt-5.4",
                        "--command",
                        sys.executable,
                        "-c",
                        "print('accuracy: 0.75')",
                    ]
                )

            creator.assert_called_once()
            self.assertEqual(creator.call_args.args[0].requested_outputs, ("experiments", "report"))
            app.advance.assert_called_once_with(max_actions=1)

    def test_research_session_cli_builds_code_task_request_from_config(self) -> None:
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            task_file = root / "task.md"
            task_file.write_text("Improve the fixture.", encoding="utf-8")
            config = root / "code_task.toml"
            config.write_text(
                "[code_task]\n"
                f'code_root = "{project.as_posix()}"\n'
                f'task_file = "{task_file.as_posix()}"\n'
                "[environment]\n"
                'mode = "external"\n'
                f'python_executable = "{Path(sys.executable).as_posix()}"\n'
                "[benchmark]\n"
                'command = "python benchmark.py"\n'
                'primary_metric = "accuracy"\n'
                "[benchmark.metric_directions]\n"
                'accuracy = "higher"\n'
                "[execute]\n"
                "use_llm = true\n"
                "timeout_sec = 7\n"
                'baseline_policy = "skip"\n',
                encoding="utf-8",
            )
            app = MagicMock()
            app.services = SimpleNamespace(max_attempts=5)
            app.view.return_value = SimpleNamespace(
                session_root=root / "session", status="completed", status_reason="",
                next_action=None, state_refs={}, attempts=(),
            )
            app.advance.return_value = app.view.return_value
            with (
                patch("simple_ar.cli.main._optional_research_llm_client", return_value=object()),
                patch(
                    "simple_ar.app.research_application.create_session",
                    return_value=app,
                ) as creator,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                main(
                    [
                        "research-session",
                        "--topic",
                        "fixture research",
                        "--output-root",
                        str(root / "sessions"),
                        "--cwd",
                        str(root),
                        "--model",
                        "gpt-5.4",
                        "--no-report",
                        "--code-task-config",
                        str(config),
                    ]
                )

            brief = creator.call_args.args[0]
            services = creator.call_args.kwargs["services"]
            execution = services.config["execution"]
            self.assertEqual(brief.requested_outputs, ("experiments",))
            self.assertIn("Improve the fixture.", brief.request_text)
            self.assertEqual(execution["cwd"], str(project.resolve()))
            self.assertEqual(execution["timeout_sec"], 7)
            self.assertEqual(execution["code_task"]["code_root"], str(project.resolve()))
            self.assertEqual(execution["code_task"]["max_repairs"], 0)
            self.assertEqual(execution["code_task"]["env_mode"], "external")
            self.assertEqual(Path(execution["code_task"]["python_executable"]), Path(sys.executable))
            original_config = config.read_text()
            for repairs, explicit, count, seconds in ((0, False, 1, 30), (1, False, 2, 60), (1, True, 0, 0)):
                with self.subTest(repairs=repairs, explicit=explicit), \
                        patch("simple_ar.cli.main._optional_research_llm_client", return_value=object()), \
                        patch("simple_ar.app.research_application.create_session", return_value=app) as created, \
                        contextlib.redirect_stdout(io.StringIO()):
                    config.write_text(original_config + f"repair_rounds = {repairs}\n")
                    main(["research-session", "--task-kind", "bug_fix", "--topic", "Repair the project",
                          "--model", "gpt-5.4", "--code-task-config", str(config), "--with-report",
                          *(["--process-invocations", "0", "--process-wall-seconds", "0"] if explicit else [])])
                    limits = created.call_args.kwargs["services"].budget_limits
                    self.assertEqual(limits["process_invocations"], count)
                    self.assertEqual(limits["process_wall_seconds"], seconds)
                    self.assertEqual(created.call_args.args[0].requested_outputs, ("bug_fix", "report"))
                    self.assertEqual(created.call_args.kwargs["services"].config["report"]["template"], "auto")
                    self.assertEqual(created.call_args.kwargs["services"].config["report"]["mode"], "supplied_materials")
            config.write_text(original_config)
            greenfield = root / "greenfield.toml"
            greenfield.write_text('[code_task]\nkind="greenfield"\n' +
                f'task_file="{task_file.as_posix()}"\n[workspace]\nmode="empty"\n' +
                '[edit_scope]\nallowed_patterns=["generated_project/**"]\n' +
                '[benchmark]\ncommand="python generated_project/main.py"\n' +
                '[execute]\nuse_llm=true\nbaseline_policy="skip"\n', encoding="utf-8")
            with patch("simple_ar.cli.main._optional_research_llm_client", return_value=object()), \
                    patch("simple_ar.app.research_application.create_session", return_value=app) as creator, \
                    contextlib.redirect_stdout(io.StringIO()):
                main(["research-session", "--topic", "Create a project", "--model", "fixture-model",
                      "--code-task-config", str(greenfield), "--outputs", "code_task", "report"])
            new_execution = creator.call_args.kwargs["services"].config["execution"]
            self.assertEqual(new_execution["code_task"]["kind"], "greenfield")
            self.assertIsNone(new_execution["code_task"]["code_root"])
            self.assertEqual(new_execution["code_task"]["workspace_mode"], "empty")
            self.assertEqual(creator.call_args.args[0].requested_outputs, ("code_task", "report"))
            reproduction = root / "reproduction.toml"
            reproduction.write_text(
                '[task]\ngoal="Check published conditions"\nkind="reproduction"\noutputs=["experiments"]\n'
                '[model]\nname="fixture-model"\n[research]\nmaterials_only=true\n'
                f'[assets]\npapers=["{task_file.as_posix()}"]\n'
                f'[execution]\ncode_task_config="{config.as_posix()}"\ncwd="{project.as_posix()}"\n'
                'command=["python","adapter.py"]\ntimeout_sec=11\n'
                '[execution.protocol]\nhypothesis="Published claim"\ndataset="Fixed data"\n'
                'expected_outcome="Compare measured accuracy"\nmetrics=["accuracy"]\n', encoding="utf-8")
            with patch("simple_ar.cli.main._optional_research_llm_client", return_value=object()), \
                 patch("simple_ar.app.research_application.create_session", return_value=app) as creator, \
                 contextlib.redirect_stdout(io.StringIO()):
                main(["research-session", "--config", str(reproduction)])
            prepared = creator.call_args.kwargs["services"].config["execution"]
            self.assertEqual(prepared["command"], ("python", "adapter.py"))
            self.assertEqual(prepared["code_task"]["validation_command"], ["python", "benchmark.py"])
            self.assertEqual(prepared["timeout_sec"], 11)
            self.assertEqual(prepared["code_task"]["validation_timeout_sec"], 7)

    def test_research_case_uses_checked_in_configs_and_case_local_paths(self) -> None:
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            case = root / "case with spaces"
            case.mkdir()
            project = case / "source project"
            data = case / "shared data"
            project.mkdir()
            data.mkdir()
            (case / "task.md").write_text("Improve this project.", encoding="utf-8")
            (case / "run.py").write_text("", encoding="utf-8")
            (case / "research.toml").write_text(
                '[task]\ngoal = "Improve this project"\noutputs = ["experiments"]\n'
                '[model]\nname = "fixture-model"\n'
                '[execution]\ncode_task_config = "code_task.toml"\n',
                encoding="utf-8",
            )
            (case / "code_task.toml").write_text(
                '[code_task]\ncode_root = "{config_dir}/source project"\n'
                'task_file = "{config_dir}/task.md"\n'
                '[environment]\nmode = "external"\n'
                f'python_executable = "{Path(sys.executable).as_posix()}"\n'
                'required_paths = ["{config_dir}/shared data"]\n'
                '[benchmark]\n'
                "command = 'python \"{config_dir}/run.py\" --data-root \"{config_dir}/shared data\"'\n"
                'primary_metric = "accuracy"\n'
                '[execute]\nuse_llm = true\nbaseline_policy = "skip"\n',
                encoding="utf-8",
            )
            app = MagicMock()
            app.services = SimpleNamespace(max_attempts=5)
            app.view.return_value = SimpleNamespace(
                session_root=root / "session", status="completed", status_reason="",
                next_action=None, state_refs={}, attempts=(),
            )
            app.advance.return_value = app.view.return_value
            with (
                patch("simple_ar.cli.main._optional_research_llm_client", return_value=object()),
                patch("simple_ar.app.research_application.create_session", return_value=app) as creator,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                main(["research-session", "--config", str(case / "research.toml")])

            execution = creator.call_args.kwargs["services"].config["execution"]
            self.assertEqual(Path(execution["cwd"]), project)
            self.assertEqual(execution["command"][0], "python")
            self.assertEqual(Path(execution["command"][1]), case / "run.py")
            self.assertEqual(Path(execution["command"][3]), data)




    def test_inspect_and_search_artifacts_commands_write_retrieval_files(self) -> None:
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            run_dir = Path(tmp) / "historical-run"
            (run_dir / "01-plan").mkdir(parents=True)
            (run_dir / "01-plan" / "problem.md").write_text(
                "# Research problem\n\nStudy reliable research agents.\n", encoding="utf-8"
            )

            inspect_stdout = io.StringIO()
            with contextlib.redirect_stdout(inspect_stdout):
                main(["inspect", str(run_dir)])

            self.assertIn("Artifacts:", inspect_stdout.getvalue())
            self.assertTrue((run_dir / "artifact_index.json").is_file())

            search_stdout = io.StringIO()
            with contextlib.redirect_stdout(search_stdout):
                main(["search-artifacts", str(run_dir), "research", "--top-k", "2"])

            self.assertIn("Matches:", search_stdout.getvalue())
            self.assertIn("Operational metadata included: False", search_stdout.getvalue())
            self.assertTrue((run_dir / "artifact_chunks.jsonl").is_file())
            self.assertTrue((run_dir / "artifact_search_results.json").is_file())

    def test_clean_removes_rebuildable_run_caches_and_shared_index_rows(self) -> None:
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / "clean-me"
            documents_dir = run_dir / "02-search" / "documents"
            cache_dir = documents_dir / "fulltext_cache"
            text_dir = documents_dir / "extracted_text"
            index_dir = run_dir / "02-search" / "research_index"
            cache_dir.mkdir(parents=True)
            text_dir.mkdir(parents=True)
            index_dir.mkdir(parents=True)
            (cache_dir / "paper.pdf").write_bytes(b"%PDF fake")
            (text_dir / "paper.txt").write_text("parsed paper text", encoding="utf-8")
            (documents_dir / "fulltext_extraction.json").write_text("{}\n", encoding="utf-8")
            (run_dir / "02-search" / "papers.jsonl").write_text("{}\n", encoding="utf-8")
            (index_dir / "chunks.jsonl").write_text("{}\n", encoding="utf-8")

            sqlite_path = root / ".simple_ar_cache" / "research_index" / "sqlite_fts.db"
            sqlite_path.parent.mkdir(parents=True)
            conn = sqlite3.connect(sqlite_path)
            conn.execute("CREATE TABLE chunks(run_id TEXT, text TEXT)")
            conn.execute("INSERT INTO chunks VALUES ('clean-me', 'delete')")
            conn.execute("INSERT INTO chunks VALUES ('other-run', 'keep')")
            conn.commit()
            conn.close()
            write_json(
                index_dir / "index_meta.json",
                {
                    "store": {"run_id": "clean-me"},
                    "sqlite_fts": {"status": "ready", "path": str(sqlite_path)},
                },
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                main(["clean", str(run_dir), "--yes"])

            self.assertFalse(cache_dir.exists())
            self.assertFalse(text_dir.exists())
            self.assertTrue((documents_dir / "fulltext_extraction.json").exists())
            self.assertTrue((run_dir / "02-search" / "papers.jsonl").exists())
            self.assertTrue((index_dir / "chunks.jsonl").exists())
            index_meta = read_json(index_dir / "index_meta.json")
            self.assertEqual(index_meta["sqlite_fts"]["status"], "cleaned")
            conn = sqlite3.connect(sqlite_path)
            rows = conn.execute("SELECT run_id FROM chunks ORDER BY run_id").fetchall()
            conn.close()
            self.assertEqual(rows, [("other-run",)])
            self.assertIn("Will delete", stdout.getvalue())
            self.assertIn("Will keep", stdout.getvalue())

    def test_clean_all_caches_removes_every_rebuildable_cache(self) -> None:
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / "clean-all"
            documents_dir = run_dir / "02-search" / "documents"
            cache_dir = documents_dir / "fulltext_cache"
            text_dir = documents_dir / "extracted_text"
            index_dir = run_dir / "02-search" / "research_index"
            code_meta = run_dir / "code_task" / "meta"
            context_dir = run_dir / "code_task" / "context_packs" / "context-001"
            report_dir = run_dir / "08-report"
            for path in (cache_dir, text_dir, index_dir, code_meta, context_dir, report_dir):
                path.mkdir(parents=True)
            (cache_dir / "paper.pdf").write_bytes(b"%PDF fake")
            (text_dir / "paper.txt").write_text("parsed paper text", encoding="utf-8")
            (documents_dir / "fulltext_extraction.json").write_text("{}\n", encoding="utf-8")
            (run_dir / "02-search" / "papers.jsonl").write_text("{}\n", encoding="utf-8")
            (index_dir / "chunks.jsonl").write_text("{}\n", encoding="utf-8")
            (run_dir / "artifact_index.json").write_text("{}\n", encoding="utf-8")
            (run_dir / "artifact_chunks.jsonl").write_text("{}\n", encoding="utf-8")
            (run_dir / "artifact_search_results.json").write_text("{}\n", encoding="utf-8")
            (code_meta / "codebase_index.json").write_text("{}\n", encoding="utf-8")
            (code_meta / "repo_map.json").write_text("{}\n", encoding="utf-8")
            (code_meta / "repo_map_summary.md").write_text("# Repo Map\n", encoding="utf-8")
            (code_meta / "locate_results.json").write_text("{}\n", encoding="utf-8")
            (code_meta / "locate_results.md").write_text("# Locate\n", encoding="utf-8")
            (context_dir / "context_pack.json").write_text("{}\n", encoding="utf-8")
            (report_dir / "report.md").write_text("# Report\n", encoding="utf-8")

            sqlite_path = root / ".simple_ar_cache" / "research_index" / "sqlite_fts.db"
            sqlite_path.parent.mkdir(parents=True)
            conn = sqlite3.connect(sqlite_path)
            conn.execute("CREATE TABLE chunks(run_id TEXT, text TEXT)")
            conn.execute("INSERT INTO chunks VALUES ('clean-all', 'delete')")
            conn.execute("INSERT INTO chunks VALUES ('other-run', 'keep')")
            conn.commit()
            conn.close()
            write_json(
                index_dir / "index_meta.json",
                {
                    "store": {"run_id": "clean-all"},
                    "sqlite_fts": {"status": "ready", "path": str(sqlite_path)},
                },
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                main(["clean", str(run_dir), "--all-caches", "--yes"])

            self.assertFalse(cache_dir.exists())
            self.assertFalse(text_dir.exists())
            self.assertFalse(index_dir.exists())
            self.assertFalse((run_dir / "artifact_index.json").exists())
            self.assertFalse((run_dir / "artifact_chunks.jsonl").exists())
            self.assertFalse((run_dir / "artifact_search_results.json").exists())
            self.assertFalse((code_meta / "codebase_index.json").exists())
            self.assertFalse((code_meta / "repo_map.json").exists())
            self.assertFalse((code_meta / "repo_map_summary.md").exists())
            self.assertFalse((code_meta / "locate_results.json").exists())
            self.assertFalse((code_meta / "locate_results.md").exists())
            self.assertFalse((run_dir / "code_task" / "context_packs").exists())
            self.assertTrue((documents_dir / "fulltext_extraction.json").exists())
            self.assertTrue((run_dir / "02-search" / "papers.jsonl").exists())
            self.assertTrue((report_dir / "report.md").exists())
            conn = sqlite3.connect(sqlite_path)
            rows = conn.execute("SELECT run_id FROM chunks ORDER BY run_id").fetchall()
            conn.close()
            self.assertEqual(rows, [("other-run",)])
            self.assertIn("All-cache cleanup is enabled", stdout.getvalue())
            self.assertIn("Deleted shared SQLite index rows: 1", stdout.getvalue())

    def test_clean_shared_index_clears_cross_run_index_store(self) -> None:
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            index_root = root / ".simple_ar_cache" / "research_index"
            lancedb_dir = index_root / "lancedb"
            index_root.mkdir(parents=True)
            lancedb_dir.mkdir()
            sqlite_path = index_root / "sqlite_fts.db"
            conn = sqlite3.connect(sqlite_path)
            conn.execute("CREATE TABLE chunks(run_id TEXT, text TEXT)")
            conn.execute("INSERT INTO chunks VALUES ('run-a', 'delete')")
            conn.commit()
            conn.close()
            (lancedb_dir / "table.lance").write_text("fake lancedb data", encoding="utf-8")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                main(["clean", "--shared-index", "--index-root", str(index_root), "--yes"])

            self.assertTrue(index_root.exists())
            self.assertEqual(list(index_root.iterdir()), [])
            self.assertIn("Shared-index cleanup is enabled", stdout.getvalue())
            self.assertIn("Cleaned targets: 2", stdout.getvalue())

    def test_clean_shared_cache_clears_index_and_literature_cache(self) -> None:
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            cache_root = root / ".simple_ar_cache"
            index_root = cache_root / "research_index"
            literature_root = cache_root / "literature"
            agent_archive_root = cache_root / "agent_handoff_archives"
            index_root.mkdir(parents=True)
            literature_root.mkdir(parents=True)
            agent_archive_root.mkdir(parents=True)
            (index_root / "chunks.sqlite").write_text("index", encoding="utf-8")
            (literature_root / "cached-provider-response.json").write_text("{}", encoding="utf-8")
            (agent_archive_root / "old-handoff" / "stderr.txt").parent.mkdir()
            (agent_archive_root / "old-handoff" / "stderr.txt").write_text("old failure", encoding="utf-8")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                main(
                    [
                        "clean",
                        "--shared-cache",
                        "--index-root",
                        str(index_root),
                        "--literature-cache-root",
                        str(literature_root),
                        "--yes",
                    ]
                )

            self.assertFalse(index_root.exists())
            self.assertFalse(literature_root.exists())
            self.assertFalse(agent_archive_root.exists())
            output = stdout.getvalue()
            self.assertIn("Shared-cache cleanup is enabled", output)
            self.assertIn("literature", output)
            self.assertIn("agent_handoff_archives", output)


    def test_code_task_init_git_worktree_error_gives_next_steps(self) -> None:
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            code_root = root / "no_git_project"
            code_root.mkdir()
            task_file = root / "task.md"
            task_file.write_text("# Task\n\nImprove this project.\n", encoding="utf-8")

            with self.assertRaises(SystemExit) as raised:
                main(
                    [
                        "code-task",
                        "init",
                        "--code-root",
                        str(code_root),
                        "--task-file",
                        str(task_file),
                        "--workspace-mode",
                        "git_worktree",
                        "--output-root",
                        str(root / "runs"),
                    ]
                )

            message = str(raised.exception)
            self.assertIn("Could not initialize code task", message)
            self.assertIn("git_worktree quick checklist", message)
            self.assertIn("--workspace-mode copy", message)

    def test_code_task_init_missing_task_file_gives_path_hint(self) -> None:
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            code_root = root / "project"
            code_root.mkdir()

            with self.assertRaises(SystemExit) as raised:
                main(
                    [
                        "code-task",
                        "init",
                        "--code-root",
                        str(code_root),
                        "--task-file",
                        str(root / "missing-task.md"),
                        "--output-root",
                        str(root / "runs"),
                    ]
                )

            message = str(raised.exception)
            self.assertIn("Check the task file path", message)
            self.assertIn("[code_task].task_file", message)


if __name__ == "__main__":
    unittest.main()
