from __future__ import annotations

import contextlib
import io
import json
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
    def test_fixed_measurement_creation_uses_common_approval_and_original_execution(self):
        from simple_ar.app.tool_application import ToolApplication
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project = root / 'original project'
            project.mkdir()
            data = project / 'observations input.tsv'
            data.write_text('value\n2\n4\n6\n', encoding='utf-8')
            script = project / 'measure.py'
            script.write_text("import os,sys,json\nfrom pathlib import Path\n"
                "values=[float(x) for x in Path(sys.argv[1]).read_text().splitlines()[1:]]\n"
                "Path('observed.json').write_text(json.dumps({'cwd':os.getcwd(),"
                "'argument':sys.argv[1], 'environment':os.environ.get('SAR_MEASUREMENT_TEST_POLICY')}))\n"
                "print('score: ' + str(sum(values)/len(values)))\n", encoding='utf-8')
            original = script.read_bytes()
            command = [sys.executable, 'measure.py', 'observations input.tsv']
            with patch('simple_ar.cli.main.sys.stdin.isatty', return_value=False), \
                    patch('simple_ar.app.tool_application.LLMClient.from_env', side_effect=AssertionError('No model')), \
                    patch('simple_ar.app.research_application.create_session', side_effect=AssertionError('No old new-task controller')), \
                    patch.dict(os.environ, {'SAR_MEASUREMENT_TEST_POLICY': 'inherited-value'}), \
                    contextlib.redirect_stdout(io.StringIO()):
                main(['research-session', '--topic', 'Measure the declared observations',
                    '--task-kind', 'measurement', '--outputs', 'experiments', '--cwd', str(project),
                    '--timeout-sec', '8', '--primary-metric', 'score', '--output-root', str(root / 'runs'),
                    '--command', *command])
                manifests = list((root / 'runs').rglob('session_manifest.json'))
                self.assertEqual(len(manifests), 1)
                app = ToolApplication.load(manifests[0].parent)
                self.assertIsNone(app.state['config']['model'])
                self.assertEqual(app.state['config']['execution_contract']['command'], command)
                self.assertIsNone(app.state['pending'])
                self.assertEqual(app.session.manifest.status, 'completed', app.session.manifest.status_reason)
                observed = read_json(project / 'observed.json')
                self.assertEqual(observed, {'cwd': str(project), 'argument': 'observations input.tsv',
                    'environment': 'inherited-value'})
                self.assertEqual(script.read_bytes(), original)
                self.assertEqual(len(app.ledger.entries), 1)
                self.assertEqual(len(app.session.list_attempts()), 1)
                payload = app.session.store.read_json(app.state['turns'][0]['tools'][0]['result'])
                self.assertEqual(payload['data']['metrics']['score'], 4)
                self.assertTrue(any(path.endswith('/analysis.md') for path in
                    app.session.store.read_json('delivery.json')['artifacts']))
                with patch('simple_ar.experiment.execution.backend.run_process') as process:
                    ToolApplication.load(app.session.store.root).run(progress=lambda _: None)
                    process.assert_not_called()

    def test_condition_set_config_routes_declarations_without_running_the_matrix(self):
        from simple_ar.cli.start import _inline_value

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            command = [sys.executable, 'measure.py']
            protocol = {'contract_id': 'cli-fixed-observations',
                'dataset_refs': [{'asset_id': 'observations'}], 'split_spec': {'name': 'all_rows'},
                'metric_specs': [{'name': 'score', 'unit': 'value'}]}
            declarations = (
                ('auto', {'command': command, 'seeds': [3, 9], 'seed_flag': '--seed'}, 4),
                ('measurement', {'command': command,
                    'protocol': {**protocol, 'comparison_conditions': {'seeds': [3, 9]}}}, 2),
                ('measurement', {'pairs': [{'seed': 3, 'baseline_command': command,
                    'candidate_command': command}]}, 2),
            )
            for index, (kind, declaration, count) in enumerate(declarations):
                model = 'fixture' if index == 1 else ''
                execution = {'cwd': str(root), 'timeout_sec': 8, 'primary_metric': 'score',
                    'baseline_policy': 'run', 'protocol': protocol, **declaration}
                config = root / f'conditions-{index}.toml'
                config.write_text('[task]\nkind=' + json.dumps(kind)
                    + '\ngoal="Compare literal conditions"\noutputs=' + ('["experiments","report"]' if model else '["experiments"]')
                    + ('\n[research]\ninteraction="autonomous"' if kind == 'auto' else '')
                    + '\n[model]\nname=' + json.dumps(model) + '\n'
                    + ('max_output_tokens=1000\n' if model else '')
                    + '[budget]\nllm_requests=4\ntotal_tokens=48000\n'
                    + 'process_invocations=4\nprocess_wall_seconds=32\n'
                    + '[execution]\n' + ''.join(f'{key}={_inline_value(value)}\n'
                        for key, value in execution.items()), encoding='utf-8')
                app = MagicMock()
                app.run.return_value = {'status': 'completed'}
                app.measurement_completion_actions.return_value = []
                with self.subTest(declaration=declaration), \
                        patch('simple_ar.cli.main._optional_research_llm_client', return_value=object()), \
                        patch('simple_ar.app.tool_application.ToolApplication.create', return_value=app) as create, \
                        patch('simple_ar.cli.main._render_native_result'), \
                        patch('simple_ar.app.research_application.create_session') as legacy, \
                        patch('subprocess.run') as process, contextlib.redirect_stdout(io.StringIO()):
                    main(['research-session', '--config', str(config)])
                create.assert_called_once()
                legacy.assert_not_called()
                process.assert_not_called()
                routed = create.call_args.kwargs
                rows = routed['execution_contract']['conditions']
                self.assertEqual(len(rows), count)
                self.assertEqual([row['role'] for row in rows], ['baseline', 'candidate'] * (count // 2))
                self.assertEqual(rows[0]['execution']['command'], command + (['--seed', '3'] if index == 0 else []))
                self.assertEqual(routed['process_limits'], {'process_invocations': 4, 'process_wall_seconds': 32})
                self.assertEqual(routed['model'], model or None)
                self.assertEqual((routed['max_turns'], routed['max_tokens'], routed['max_output_tokens']),
                    (4, 48000, 1000) if model else (0, 0, None))
                self.assertEqual(routed['report_requirement']['method'] if model else routed['report_requirement'],
                    'article' if model else None)
                app.run.assert_called_once()
                self.assertNotIn('approved_execution', app.run.call_args.kwargs)

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

    @contextlib.contextmanager
    def _structured_creation(self):
        # Observe the public intake boundary, never simulate legacy creation.
        with patch("simple_ar.cli.start.migrate_structured_creation") as migrate, \
                patch("simple_ar.cli.main._optional_research_llm_client",
                      side_effect=lambda model, *a, **k: object() if model else None), \
                patch("simple_ar.app.research_application.create_session",
                      side_effect=AssertionError("New tasks must use ToolApplication")), \
                contextlib.redirect_stdout(io.StringIO()):
            yield migrate

    def test_structured_new_routes_outputs_roles_and_table_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper, notes, table = [root / name for name in ("paper.md", "notes.md", "values.csv")]
            paper.write_text("# Evidence\nReported observations.", encoding="utf-8")
            notes.write_text("User interpretation.", encoding="utf-8")
            table.write_text("group,value\na,2\na,4\n", encoding="utf-8")
            cases = (
                (["--no-report"], ("summary",), "paper", None),
                (["--outputs", "experiments"], ("experiments",), "paper", None),
                (["--model", "fixture"], ("report",), "paper", None),
                (["--task-kind", "survey", "--outputs", "summary", "--model", "fixture"],
                 ("summary",), "paper", None),
                (["--task-kind", "writing", "--material", str(notes), "--model", "fixture",
                  "--report-template", "experiment"], ("report",), "material", None),
                (["--task-kind", "data_analysis", "--data-file", str(table),
                  "--value-column", "value", "--group-column", "group", "--observation-unit", "record"],
                 ("data_analysis",), "dataset", "value"),
                (["--task-kind", "data_analysis", "--data-file", str(table),
                  "--value-column", "value", "--observation-unit", "record", "--outputs", "data_analysis", "report",
                  "--model", "fixture"], ("data_analysis", "report"), "dataset", "value"),
            )
            for flags, outputs, role, column in cases:
                documents = [] if role == "dataset" and "report" not in outputs else [
                    "--local-document", str(paper)]
                with self.subTest(flags=flags), self._structured_creation() as migrate:
                    main(["research-session", "--topic", "Use declared evidence",
                          "--output-root", str(root / "runs"), *documents, *flags])
                migrate.assert_called_once()
                args = migrate.call_args.args[0]
                routed = migrate.call_args.kwargs
                self.assertIsNone(args.command_argv)
                self.assertEqual(routed["request"], "Use declared evidence")
                self.assertEqual(tuple(routed["outputs"]), outputs)
                self.assertEqual(routed["root"].parent, root / "runs")
                self.assertNotIn("execution", routed["research_config"])
                if 'experiments' not in outputs:
                    self.assertEqual({k: routed["limits"][k] for k in (
                        "process_invocations", "process_wall_seconds")},
                        {"process_invocations": 0, "process_wall_seconds": 0})
                source = next(row for row in routed["sources"] if row["role"] == role)
                self.assertEqual(source["mutability"], "read_only")
                self.assertEqual(source["allowed_uses"], ["read"] if role == "dataset" else ["read", "reference"])
                if column:
                    self.assertEqual(routed["data_analysis"]["file"], str(table.resolve()))
                    self.assertEqual(list(routed["data_analysis"]["value_columns"]), [column])
                else:
                    self.assertIsNone(routed["data_analysis"])
                if role == "material":
                    self.assertEqual(source["locator"], str(notes))
                    self.assertEqual(routed["report_config"]["template"], "experiment")
                if "report" in outputs:
                    self.assertEqual(routed["writing_method"], "article")
                    self.assertEqual(routed["report_config"]["reviewer"], "disabled")
                    self.assertFalse(routed["report_config"]["document_review"])
                    self.assertEqual(routed["report_config"]["mode"], "research_only")
                    if role != "material":
                        self.assertEqual(routed["report_config"]["template"], "auto")

    def test_structured_deterministic_delivery_is_native_and_resume_does_not_replay(self):
        from simple_ar.app.tool_application import ToolApplication

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper, table = root / "paper.md", root / "values.csv"
            paper.write_text("# Method\nValidation supports reliability.\n", encoding="utf-8")
            table.write_text("value\n2\n4\n6\n", encoding="utf-8")
            config = root / "local.toml"
            config.write_text("[research]\nmaterials_only=true\n", encoding="utf-8")
            for flags, contract in (
                (["research-brief", "--config", str(config), "--provider", "local_files",
                  "--local-document", str(paper)], "research_policy"),
                (["research-session", "--task-kind", "data_analysis", "--data-file", str(table),
                  "--value-column", "value", "--observation-unit", "record"], "table_analysis"),
            ):
                output = root / contract
                with self.subTest(contract=contract), \
                        patch("simple_ar.app.tool_application.LLMClient.from_env",
                              side_effect=AssertionError("Deterministic work needs no model")), \
                        patch("simple_ar.app.research_application.create_session",
                              side_effect=AssertionError("No legacy creation")), \
                        contextlib.redirect_stdout(io.StringIO()):
                    main([*flags, "--topic", "Describe supplied evidence", "--output-root", str(output)])
                    manifest, = output.rglob("session_manifest.json")
                    app = ToolApplication.load(manifest.parent)
                    self.assertEqual(app.session.manifest.transition_recipe, ToolApplication.RECIPE)
                    self.assertEqual(app.session.manifest.status, "completed")
                    self.assertIsNotNone(app.state["config"][contract])
                    self.assertIsNone(app.state["config"]["model"])
                    self.assertFalse(app.state["config"]["allow_execution"])
                    delivery = app.session.store.read_json("delivery.json")
                    self.assertTrue(delivery["artifacts"])
                    before = (app.ledger.to_dict(), app.state["turns"])
                    with patch("simple_ar.tools.sources.SourceToolGateway.call") as sources, \
                            patch("simple_ar.tools.analysis.AnalysisToolGateway.call") as analysis:
                        main(["start", "--resume-task", str(manifest.parent)])
                    sources.assert_not_called()
                    analysis.assert_not_called()
                    restored = ToolApplication.load(manifest.parent)
                    self.assertEqual((restored.ledger.to_dict(), restored.state["turns"]), before)
                    if contract == 'table_analysis':
                        with patch('simple_ar.tools.analysis.AnalysisToolGateway.call',
                                   side_effect=AssertionError('Text feedback must not recalculate')):
                            restored.run(feedback='Retain the calculation; deliver the existing package', progress=lambda _: None)
                            status = restored.run(actions=[{'tool_name': 'deliver_result', 'arguments': {
                                'summary': 'Existing calculation retained', 'artifacts': delivery['artifacts']}}], progress=lambda _: None)
                        self.assertEqual(status['status'], 'completed')
                        self.assertEqual(restored.ledger.to_dict(), before[0])

    def test_survey_retained_sources_keep_material_role_and_reject_plain_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle, notes = root / "sources.json", root / "notes.md"
            notes.write_text("User notes", encoding="utf-8")
            write_json(bundle, {"schema_version": "document_bundle.v1", "documents": [{
                "document_id": "source", "source": "arxiv", "source_id": "1234.5678",
                "title": "Original source", "kind": "paper", "evidence_role": "original_source"}],
                "sections": [], "chunks": []})
            with self._structured_creation() as migrate:
                main(["research-session", "--topic", "Investigate evidence", "--task-kind", "survey",
                      "--material", str(bundle), "--model", "fixture", "--output-root", str(root / "runs")])
                self.assertEqual(migrate.call_args.kwargs["sources"][0]["role"], "material")
                migrate.reset_mock()
                with self.assertRaisesRegex(SystemExit, "original-source"):
                    main(["research-session", "--topic", "Investigate evidence", "--task-kind", "survey",
                          "--material", str(notes)])
                migrate.assert_not_called()

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

    def test_structured_design_and_source_controls_preserve_merged_configuration(self):
        from simple_ar.cli.start import _inline_value
        from simple_ar.cli.research_config import research_defaults

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "research.toml"
            research = {"interaction": "assisted", "queries": ["declared query"],
                "providers": ["local_files"], "materials_only": True, "use_fulltext": False,
                "allow_pdf_download": False, "keep_raw_pdf": False, "web_extract_backend": "direct",
                "max_results": 2, "max_chunks": 20, "idea_limit": 1, "max_iterations": 3,
                "read_max_shortlist": 4, "max_pdf_pages": 5, "max_fulltext_documents": 6,
                "max_pdf_mb": 7, "cache_dir": str(root / "cache")}
            config.write_text('[task]\ngoal="Review a method"\noutputs=["experiments"]\n'
                'selected_idea_id="candidate-2"\n[model]\nname="author"\n'
                'feasibility_review_model="reviewer"\nmax_output_tokens=321\n'
                '[budget]\nllm_requests=4\ntotal_tokens=12345\n'
                'process_invocations=0\nprocess_wall_seconds=0\n[research]\n'
                + "".join(f"{key}={_inline_value(value)}\n" for key, value in research.items()),
                encoding="utf-8")
            defaults = research_defaults(["research-session", "--config", str(config)])
            self.assertEqual(defaults["feasibility_review_model"], "reviewer")
            with self._structured_creation() as migrate:
                main(["research-session", "--config", str(config), "--output-root", str(root / "runs"),
                      "--max-results", "9"])
            args, = migrate.call_args.args
            routed = migrate.call_args.kwargs
            self.assertEqual((args.model, args.max_output_tokens, args.max_results, args.max_chunks,
                              args.idea_limit), ("author", 321, 9, 20, 1))
            self.assertEqual(routed["review_model"], "reviewer")
            self.assertEqual(tuple(routed["outputs"]), ("experiments",))
            saved = routed["research_config"]
            for key, value in {"interaction": "assisted", "research_selected_idea_id": "candidate-2",
                    "research_queries": ["declared query"], "research_sources": ["local_files"],
                    "research_max_documents": 9, "research_max_iterations": 3,
                    "research_read_max_shortlist": 4, "research_max_pdf_pages": 5,
                    "research_max_fulltext_documents": 6, "research_max_pdf_mb": 7,
                    "research_materials_only": True, "research_use_fulltext": False,
                    "research_allow_pdf_download": False, "research_keep_raw_pdf": False,
                    "research_web_extract_backend": "direct"}.items():
                self.assertEqual(saved[key], value, key)
            self.assertEqual(routed["limits"], {"llm_requests": 4, "total_tokens": 12345,
                "process_invocations": 0, "process_wall_seconds": 0})

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
            ResearchApplicationServices, create_session, load_session,
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
            response = app.controller.store.read_json(
                f"outputs/research-decision-{decision_id}-response.json",
            )
            self.assertEqual(response["interaction"]["response"]["revision"], {
                "report": {"template": "analysis_report"},
            })
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


    def test_research_session_interrupted_recovery_requires_saved_session(self) -> None:
        args = build_parser(allow_resume_without_topic=True).parse_args([
            "research-session", "--session-root", "saved", "--recover-interrupted",
        ])
        self.assertTrue(args.recover_interrupted)
        with self.assertRaisesRegex(SystemExit, "requires --session-root"):
            main(["research-session", "--topic", "test", "--recover-interrupted"])

    def test_historical_code_task_cli_routes_saved_execution_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
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

    def test_brief_alias_uses_structured_summary_and_keeps_compatibility_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = root / "source.md"
            paper.write_text("# Method\nReliable agents use validation.\n", encoding="utf-8")
            with self._structured_creation() as migrate:
                main(["research-brief", "--topic", "reliable agents", "--local-document", str(paper),
                      "--query", "validation", "--provider", "local_files", "--max-results", "2",
                      "--max-chunks", "20", "--idea-limit", "1", "--cache-dir", str(root / "cache")])
            args = migrate.call_args.args[0]
            routed = migrate.call_args.kwargs
            self.assertEqual(tuple(routed["outputs"]), ("summary",))
            self.assertEqual((args.model, args.interaction, args.output_root),
                             (None, "autonomous", "runs/research-brief"))
            self.assertEqual((args.max_results, args.max_chunks, args.idea_limit), (2, 20, 1))
            self.assertEqual(Path(args.cache_dir), root / "cache")
            self.assertEqual(routed["research_config"]["research_queries"], ["validation"])
            self.assertEqual(routed["research_config"]["research_sources"], ["local_files"])
            self.assertEqual(routed["sources"][0]["locator"], str(paper))
        with patch("simple_ar.cli.main._print_research_session") as run:
            main(["research-brief", "--topic", "Read evidence", "--model", "profile:reader"])
            self.assertEqual(run.call_args.args[0].model, "profile:reader")
            for flags in (["--outputs", "report"], ["--session-root", "saved"]):
                with self.subTest(flags=flags), self.assertRaisesRegex(SystemExit, "summary only"):
                    main(["research-brief", "--topic", "Read evidence", *flags])



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
            migration = ArtifactStore(successor).read_json(manifest['state_refs']['legacy_import']['path'])
            self.assertTrue((successor / migration['imported'][0]['destination_ref']['path']).is_file())

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


    def test_research_report_cli_routes_existing_evidence_and_refresh(self) -> None:
        args = build_parser().parse_args([
            "research-report", "--session-root", "session", "--model", "env", "--draft-scope", "document"])
        self.assertEqual(args.draft_scope, "document")
        with tempfile.TemporaryDirectory() as tmp:
            session_root = Path(tmp) / "existing-session"
            session_root.mkdir()
            final_view = SimpleNamespace(session_root=session_root, status="completed",
                status_reason="", next_action=None,
                state_refs={"report": SimpleNamespace(path="attempts/report-001/report.md")},
                attempts=())
            for flags, expected in (
                ([], {}),
                (["--template", "survey"], {
                    "report_config": {"template": "survey"},
                    "reason": "Apply explicit report settings and rebuild only report deliverables from existing evidence."}),
                (["--refresh"], {"refresh": True, "report_config": {},
                    "reason": "Explicitly regenerate the report from existing research evidence."}),
            ):
                with self.subTest(flags=flags):
                    saved = {"report_writing_method": "article", "report": {
                        "reviewer": "llm", "document_review": True, "review_scope": "document",
                        "draft_scope": "auto", "max_review_iterations": 3, "allow_source_backtracking": False}}
                    app = MagicMock()
                    app.brief = SimpleNamespace(requested_outputs=("experiments",))
                    app.services = SimpleNamespace(max_attempts=4, config=saved.copy())
                    app.controller.manifest.budget.max_attempts = 4
                    app.view.return_value = SimpleNamespace(session_root=session_root,
                        status="completed", status_reason="", next_action=None,
                        state_refs={}, attempts=())
                    app.request_report.return_value = SimpleNamespace(session_root=session_root,
                        status="running", status_reason="", next_action=None,
                        state_refs={}, attempts=())
                    app.advance.return_value = final_view
                    stdout = io.StringIO()
                    with patch("simple_ar.cli.main._optional_research_llm_client", return_value=object()), \
                            patch("simple_ar.app.research_application.load_session", return_value=app) as loader, \
                            patch("simple_ar.app.research_application.create_session",
                                  side_effect=AssertionError("must not create a new session")), \
                            contextlib.redirect_stdout(stdout):
                        main(["research-report", "--session-root", str(session_root),
                              "--model", "gpt-5.4", *flags])
                    loader.assert_called_once()
                    self.assertEqual(loader.call_args.args[0], session_root)
                    app.request_report.assert_called_once_with(**expected)
                    self.assertEqual(app.services.config, saved)
                    self.assertEqual(loader.call_args.kwargs["services"].config, {})
                    app.advance.assert_called_once_with(max_actions=1)
                    self.assertIn("Status: completed", stdout.getvalue())
                    self.assertIn(str(session_root / "attempts/report-001/report.md"), stdout.getvalue())

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
            app.budget_ledger = SimpleNamespace(
                limits={"process_invocations": 0, "process_wall_seconds": 0},
                remaining=lambda _name: None,
            )
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
            self.assertNotIn("process_invocations=", str(raised.exception))
            self.assertNotIn("process_wall_seconds=", str(raised.exception))

    def test_research_session_cli_routes_recovery_and_explicit_allowances(self):
        from simple_ar.app.research_application import ResearchApplicationServices
        from simple_ar.research.workflow_contracts import ResearchBrief

        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "session"
            session.mkdir()
            app = MagicMock()
            app.brief = ResearchBrief(request_text="Review calibration.",
                objective="Review calibration.", requested_outputs=("report",))
            app.services = ResearchApplicationServices(config={})
            app.controller.manifest.budget.max_attempts = 5
            completed = SimpleNamespace(session_root=session, status="completed",
                status_reason="", next_action=None, state_refs={}, attempts=())
            app.view.return_value = app.advance.return_value = completed
            base = ["research-session", "--session-root", str(session),
                    "--topic", app.brief.objective]
            cases = (
                ([], False, None, None, None),
                (["--recover-interrupted"], True, None, None, None),
                (["--authorization-id", "report-refresh-1",
                  "--authorization-reason", "One bounded refresh.",
                  "--additional-attempts", "4", "--additional-no-progress", "1",
                  "--authorize-remaining", "total_tokens=200"], False, 4, 1,
                 {"total_tokens": 200}),
                (["--additional-attempts", "0", "--additional-no-progress", "0"],
                 False, 0, 0, None),
            )
            with patch("simple_ar.cli.main._optional_research_llm_client", return_value=None), \
                    patch("simple_ar.app.research_application.load_session", return_value=app), \
                    patch("simple_ar.cli.start.migrate_structured_creation",
                          side_effect=AssertionError("Historical continuation must retain its owner")), \
                    contextlib.redirect_stdout(io.StringIO()):
                for flags, recover, attempts, no_progress, resources in cases:
                    with self.subTest(flags=flags):
                        app.continue_session.reset_mock()
                        main(base + flags)
                        app.continue_session.assert_called_once()
                        routed = app.continue_session.call_args.kwargs
                        self.assertEqual(routed["recover_interrupted"], recover)
                        self.assertEqual(routed["additional_attempts"], attempts)
                        self.assertEqual(routed["additional_no_progress"], no_progress)
                        self.assertEqual(routed["authorize_remaining"], resources)
                        self.assertIsNone(routed["report_writing_method"])
                        if resources:
                            self.assertEqual(routed["authorization_id"], "report-refresh-1")
                            self.assertEqual(routed["authorization_reason"], "One bounded refresh.")

    def test_structured_report_saves_common_writer_contract_before_generation(self):
        from simple_ar.app.tool_application import ToolApplication

        client = MagicMock()
        client._settings = SimpleNamespace(api_mode="chat")
        client.connection_binding.return_value = {"profile": "fixture", "settings": {"model": "fixture", "api_mode": "chat"}}
        with tempfile.TemporaryDirectory() as tmp, \
                patch("simple_ar.cli.main._optional_research_llm_client", return_value=client), \
                patch("simple_ar.app.tool_application.LLMClient.from_env", return_value=client), \
                patch.object(ToolApplication, "run", return_value={"status": "completed"}) as run, \
                patch("simple_ar.cli.main._render_native_result"), \
                contextlib.redirect_stdout(io.StringIO()):
            main(["research-session", "--topic", "Review evidence", "--model", "fixture", "--output-root", tmp,
                  "--max-output-tokens", "321", "--report-writing-method", "sections",
                  "--report-outline-strategy", "adaptive", "--report-document-review",
                  "--max-section-tokens", "123", "--max-review-iterations", "3",
                  "--max-document-review-prompt-chars", "4567"])
            manifest, = Path(tmp).rglob("session_manifest.json")
            app = ToolApplication.load(manifest.parent)
            requirement = app.state["config"]["report_requirement"]
            self.assertEqual(requirement["method"], "sections")
            self.assertEqual({key: requirement["config"][key] for key in (
                "outline_strategy", "document_review", "max_section_tokens",
                "max_review_iterations", "max_document_review_prompt_chars")},
                {"outline_strategy": "adaptive", "document_review": True, "max_section_tokens": 123,
                 "max_review_iterations": 3, "max_document_review_prompt_chars": 4567})
            self.assertEqual(app.state["config"]["max_output_tokens"], 321)
            run.assert_called_once()
            client.chat_turn.assert_not_called()

    def test_structured_report_preflight_precedes_model_and_creation(self):
        with patch("simple_ar.cli.main._optional_research_llm_client") as client, \
                patch("simple_ar.cli.start.migrate_structured_creation") as migrate:
            with self.assertRaisesRegex(SystemExit, "requires review_scope"):
                main(["research-session", "--topic", "topic", "--report-draft-scope", "document"])
            client.assert_not_called()
            migrate.assert_not_called()


    def test_research_session_cli_builds_code_task_request_from_config(self) -> None:
        from simple_ar.app.tool_application import ToolApplication
        from simple_ar.tools.scientific import measurement_conditions
        app = MagicMock()
        app.run.return_value = {'status': 'completed'}
        def capture_creation(root, goal, **options):
            app.state = {'config': options, 'turns': [], 'pending': None}
            return app
        renderer = patch('simple_ar.cli.main._render_native_result')
        renderer.start()
        self.addCleanup(renderer.stop)
        # Current creation consumes canonical options; the old public Python
        # bridge remains compatible, but must not be a production middle layer.
        bridge = patch('simple_ar.experiment.code_task_bridge.spec.code_task_project_spec',
                       side_effect=AssertionError('Do not reconstruct a second experiment spec'))
        bridge.start()
        self.addCleanup(bridge.stop)
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
                "max_file_bytes = 64\n"
                "[workspace]\n"
                'mode = "sparse_copy"\n'
                'include = ["src/**", "benchmark.py"]\n'
                'exclude = ["src/private/**"]\n'
                "reuse_source_venv = true\n"
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
            fake = MagicMock()
            fake._settings = SimpleNamespace(api_mode='chat', context_input_tokens=24000)
            fake.connection_binding.return_value = {'profile': 'fixture',
                'settings': {'model': 'fixture', 'api_mode': 'chat'}}
            fake.effective_output_limit.return_value = None
            fake.with_output_limit.return_value = fake
            fake.chat_turn.return_value = {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': 'clarify', 'type': 'function', 'function': {'name': 'ask_user',
                'arguments': json.dumps({'question': 'Confirm the declared preparation before execution.'})}}]}
            for policy, declaration, expected in (
                    ('skip', 'seeds=[7]\nseed_flag="--seed"\n', [('candidate', 7)]),
                    ('run', 'seeds=[7,9]\nseed_flag="--seed"\n',
                     [('baseline', 7), ('candidate', 7), ('baseline', 9), ('candidate', 9)]),
                    ('skip', '[[execution.pairs]]\nseed=7\nbaseline_command=["python","benchmark.py","--baseline"]\n'
                     'candidate_command=["python","benchmark.py","--candidate"]\n', [('candidate', 7)])):
                public = root / 'research.toml'
                public.write_text(f'[execution]\ncode_task_config="{config.as_posix()}"\n'
                    f'baseline_policy="{policy}"\n' + declaration, encoding='utf-8')
                with self.subTest(policy=policy, conditions=expected), \
                        patch('simple_ar.cli.main._optional_research_llm_client', return_value=object()), \
                        patch('simple_ar.app.tool_application.ToolApplication.create', wraps=ToolApplication.create) as create, \
                        patch('simple_ar.app.tool_application.LLMClient.from_env', return_value=fake), \
                        patch('subprocess.run') as process, \
                        patch('sys.stdin.isatty', return_value=False), \
                        patch('simple_ar.app.research_application.create_session') as old, \
                        contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(SystemExit, 'Code task ended with status'):
                        main(['research-session', '--config', str(public), '--topic', 'fixture research',
                              '--model', 'fixture-model', '--no-report', '--output-root', str(root / 'sessions')])
                old.assert_not_called()
                create.assert_called_once()
                fake.chat_turn.assert_called_once()
                process.assert_not_called()
                routed = create.call_args.kwargs
                state = read_json(create.call_args.args[0] / 'actions.json')
                self.assertEqual(state['config']['execution_contract'], routed['execution_contract'])
                self.assertEqual(len(state['turns']), 1)
                self.assertIsNone(state['pending'])
                fake.chat_turn.reset_mock()
                rows = routed['execution_contract']['conditions']
                self.assertEqual([(row['role'], row['seed']) for row in rows], expected)
                self.assertEqual([row['source'] for row in rows],
                    ['preparation' if role == 'baseline' else 'implementation' for role, _ in expected])
                self.assertEqual([row.get('before_implementation', False) for row in rows],
                    [role == 'baseline' for role, _ in expected])
                self.assertTrue(all(row['execution']['cwd'] == str(project.resolve()) for row in rows))
                self.assertTrue(all(row['execution']['timeout_sec'] == 7 for row in rows))
                self.assertEqual(rows[0]['execution']['command'][0], str(Path(sys.executable).absolute()))
                self.assertIn('Improve the fixture.', create.call_args.args[1])
                self.assertEqual(routed['code_task']['validation_command'], ['python', 'benchmark.py'])
                self.assertEqual(state['turns'][0]['tools'][0]['id'], 'clarify')
                self.assertGreaterEqual(routed['max_attempts'], 20)
                self.assertEqual(list(routed['sources'].values()), [project.resolve()])
                self.assertTrue(routed['allow_execution'])
                self.assertEqual(routed['code_task']['allowed_patterns'], ['**'])
                self.assertEqual({key: routed['code_task']['preparation'][key] for key in (
                    'workspace_mode', 'max_file_bytes', 'workspace_include', 'workspace_exclude',
                    'workspace_reuse_source_venv', 'python_executable', 'max_repairs')},
                    {'workspace_mode': 'sparse_copy', 'max_file_bytes': 64,
                     'workspace_include': ['src/**', 'benchmark.py'], 'workspace_exclude': ['src/private/**'],
                     'workspace_reuse_source_venv': True, 'python_executable': str(Path(sys.executable)),
                     'max_repairs': 0})
            # Raw normalized inputs are reused only by the adapter-boundary
            # assertions below; the public new-creation consumer above is native.
            from simple_ar.app.research_execution import code_task_execution_options
            from simple_ar.cli.main import _load_code_task_options_for_cli
            execution = {'code_task': code_task_execution_options(*_load_code_task_options_for_cli(config)),
                'result_schema': rows[0]['execution']['result_schema']}
            self.assertEqual(execution['result_schema']['primary_metric'], 'accuracy')
            self.assertEqual(execution['result_schema']['required_metrics'], ['accuracy'])
            self.assertEqual(execution['result_schema']['metric_directions'], {'accuracy': 'higher_is_better'})
            original_config = config.read_text()
            for repairs, explicit, count, seconds in ((0, False, 1, 7), (1, False, 2, 14), (1, True, 0, 0)):
                with self.subTest(repairs=repairs, explicit=explicit), \
                        patch("simple_ar.cli.main._optional_research_llm_client", return_value=object()), \
                        patch.object(ToolApplication, 'create', side_effect=capture_creation) as create, \
                        patch("simple_ar.app.research_application.create_session") as created, \
                        contextlib.redirect_stdout(io.StringIO()):
                    config.write_text(original_config + f"repair_rounds = {repairs}\n")
                    main(["research-session", "--task-kind", "bug_fix", "--topic", "Repair the project",
                          "--model", "gpt-5.4", "--code-task-config", str(config), "--with-report",
                          *(["--process-invocations", "0", "--process-wall-seconds", "0"] if explicit else [])])
                    created.assert_not_called()
                    routed = create.call_args.kwargs
                    self.assertEqual(routed['process_limits'], {'process_invocations': count, 'process_wall_seconds': seconds})
                    self.assertEqual(routed['code_task']['preparation']['max_repairs'], repairs)
                    self.assertEqual(routed['report_requirement']['method'], 'article')
                    self.assertEqual(routed['report_requirement']['config']['template'], 'auto')
                    self.assertEqual(routed['report_requirement']['config']['mode'], 'supplied_materials')
            config.write_text(original_config)
            greenfield = root / "greenfield.toml"
            # Keep the source-less CLI branch here. Scaffold mode combinations
            # and physical preparation belong to the implementation owner.
            greenfield.write_text('[code_task]\nkind="greenfield"\n' +
                f'task_file="{task_file.as_posix()}"\n' +
                '[workspace]\nmode="empty"\n' +
                '[edit_scope]\nallowed_patterns=["generated_project/**"]\nprotected_patterns=["check.py"]\n' +
                '[benchmark]\ncommand="python generated_project/main.py"\n' +
                '[execute]\nuse_llm=true\nbaseline_policy="skip"\n', encoding="utf-8")
            public.write_text(f'[execution]\ncode_task_config="{greenfield.as_posix()}"\n'
                'baseline_policy="skip"\n[[execution.pairs]]\nseed=7\n'
                'baseline_command=["python","-m","baseline_package"]\n'
                'candidate_command=["python","-m","generated_project"]\n', encoding='utf-8')
            for options in (['--code-task-config', str(greenfield), '--outputs', 'code_task'],
                            ['--config', str(public), '--outputs', 'experiments']):
                with self.subTest(greenfield_options=options), \
                        patch('simple_ar.cli.main._optional_research_llm_client', return_value=object()), \
                        patch.object(ToolApplication, 'create', side_effect=capture_creation) as create, \
                        patch('simple_ar.app.research_application.create_session') as legacy, \
                        contextlib.redirect_stdout(io.StringIO()):
                    main(['research-session', '--topic', 'Create a project', '--model', 'fixture', *options])
                legacy.assert_not_called()
                create.assert_called_once()
                routed = create.call_args.kwargs
                exact = routed['code_task']
                self.assertEqual(exact['project_path'], '')
                self.assertEqual(list(routed['sources'].values()), [])
                self.assertEqual(exact['preparation']['kind'], 'greenfield')
                self.assertEqual(exact['preparation']['workspace_mode'], 'empty')
                self.assertEqual(exact['allowed_patterns'], ['generated_project/**'])
                self.assertEqual(exact['protected_patterns'], ['check.py'])
                self.assertEqual(exact['validation_command'], ['python', 'generated_project/main.py'])
                self.assertNotIn('approve', app.run.call_args.kwargs)
                self.assertIsNone(routed['report_requirement'])
                if 'experiments' in options:
                    self.assertEqual([row['source'] for row in routed['execution_contract']['conditions']],
                        ['implementation'])  # Generated candidate, without a suffix-based source heuristic.
            from simple_ar.cli.start import migrate_exact_code_creation
            config.write_text(original_config.replace('mode = "sparse_copy"', 'mode = "copy"')
                + '\n[edit_scope]\nallowed_patterns=["adapter.py"]\n', encoding='utf-8')
            reproduction = root / "reproduction.toml"
            for kind, environment, report in (('reproduction', None, False),
                    ('auto', 'current', False), ('auto', 'venv', True)):
                # Public config/main owns intake; preparation and required-file
                # execution are exercised by the implementation domain tests.
                config.write_text((original_config.replace('mode = "sparse_copy"', 'mode = "copy"')
                    if environment is None else original_config.replace('mode = "sparse_copy"', 'mode = "copy"')
                        .replace('mode = "external"', 'mode = "current"')
                        .replace(f'python_executable = "{Path(sys.executable).as_posix()}"\n', '')
                        .replace('reuse_source_venv = true', 'reuse_source_venv = false'))
                    + '\n[edit_scope]\nallowed_patterns=["adapter.py"]\n', encoding='utf-8')
                reproduction.write_text(
                    f'[task]\ngoal="Check declared conditions"\nkind="{kind}"\noutputs=["experiments"]\n'
                    '[model]\nname="fixture-model"\n[research]\nmaterials_only=true\n'
                    '[budget]\nllm_requests=4\ntotal_tokens=10000\n'
                    + ('process_invocations=3\nprocess_wall_seconds=30\n' if environment is None else '')
                    + f'[assets]\npapers=["{task_file.as_posix()}"]\n'
                    + f'[execution]\ncode_task_config="{config.as_posix()}"\ncwd="{project.as_posix()}"\n'
                    + ('command=["python","adapter.py"]\n' if kind == 'reproduction' else '')
                    + 'timeout_sec=11\ninitial_files=["adapter.py"]\n'
                    '[execution.protocol]\nhypothesis="Declared claim"\ndataset="Fixed data"\n'
                    'expected_outcome="Compare measured accuracy"\nmetrics=["accuracy"]\n'
                    + (f'[execution.environment]\nmode="{environment}"\ntimeout_sec=400\n'
                        'check_command=["python","-c","import math"]\n' if environment else ''), encoding="utf-8")
                with self.subTest(kind=kind, environment=environment), \
                        patch('simple_ar.cli.main._optional_research_llm_client', return_value=object()), \
                        patch.object(ToolApplication, 'create', side_effect=capture_creation) as create, \
                        patch('simple_ar.app.research_application.create_session') as legacy, \
                        patch('subprocess.run') as process, contextlib.redirect_stdout(io.StringIO()):
                    main(['research-session', '--config', str(reproduction), '--outputs', 'experiments',
                          *(['report'] if report else []),
                          *(['--process-invocations', '0', '--process-wall-seconds', '0'] if environment == 'venv' else [])])
                legacy.assert_not_called()
                process.assert_not_called()
                create.assert_called_once()
                routed = create.call_args.kwargs
                formal = routed['execution_contract']
                selected = measurement_conditions(formal)[0]['execution']
                self.assertEqual(selected['cwd'], str(project.resolve()))
                self.assertEqual(selected['result_schema'], execution['result_schema'])
                self.assertEqual(selected['protocol']['dataset'], 'Fixed data')
                if kind == 'reproduction':
                    self.assertEqual([row['source'] for row in formal['conditions']], ['implementation'])
                    self.assertEqual(selected['command'], [str(Path(sys.executable).absolute()), 'adapter.py'])
                    self.assertEqual(selected['timeout_sec'], 11)
                else:
                    self.assertEqual([row['source'] for row in formal['conditions']], ['implementation'])
                    self.assertEqual(routed['code_task']['environment']['mode'], environment)
                    self.assertEqual(routed['code_task']['environment']['check_command'], ['python', '-c', 'import math'])
                    self.assertNotIn('environment', formal)
                    if environment == 'venv':
                        self.assertEqual(selected['command'], ['python', 'benchmark.py'])
                self.assertEqual(routed['code_task']['validation_command'], ['python', 'benchmark.py'])
                self.assertTrue(routed['allow_execution'])
                self.assertFalse(routed['allow_network'])
                self.assertEqual(routed['code_task']['timeout_sec'], 7 if kind == 'reproduction' else 11)
                self.assertEqual(routed['code_task']['new_files'], ['adapter.py'])
                self.assertNotIn('initial_files', routed['code_task'])
                self.assertNotIn('initial_files', routed['code_task']['preparation'])
                self.assertNotIn('new_files', routed['code_task']['preparation'])
                self.assertFalse((project / 'adapter.py').exists())
                self.assertEqual(list(routed['sources'].values()), [project.resolve(), task_file.resolve()])
                self.assertEqual(routed['code_task']['preparation']['workspace_mode'], 'copy')
                self.assertEqual((routed['model'], routed['max_turns'], routed['max_tokens']), ('fixture-model', 4, 10000))
                if environment == 'current':
                    # Default allowance includes the check and a separate validator.
                    self.assertGreaterEqual(routed['process_limits']['process_invocations'], 3)
                    self.assertGreaterEqual(routed['process_limits']['process_wall_seconds'], 422)
                else:
                    self.assertEqual(routed['process_limits'], {'process_invocations': 0, 'process_wall_seconds': 0}
                        if environment == 'venv' else {'process_invocations': 3, 'process_wall_seconds': 30})
                self.assertNotIn('approve', app.run.call_args.kwargs)
                self.assertEqual(routed['report_requirement']['config']['template'] if report else routed['report_requirement'],
                                 'auto' if report else None)
                if kind == 'reproduction':
                    strict_formal = selected
            # The same public declarations also apply to code-only work.
            reproduction.write_text(reproduction.read_text().split('[execution.protocol]')[0]
                .replace('outputs=["experiments"]', 'outputs=["code_task"]')
                + '[execution.environment]\nmode="current"\ntimeout_sec=400\n'
                'check_command=["python","-c","import math"]\n', encoding='utf-8')
            with patch('simple_ar.cli.main._optional_research_llm_client', return_value=object()), \
                    patch.object(ToolApplication, 'create', side_effect=capture_creation) as create, \
                    patch('simple_ar.app.research_application.create_session') as legacy, \
                    patch('subprocess.run') as process, contextlib.redirect_stdout(io.StringIO()):
                main(['research-session', '--config', str(reproduction)])
            create.assert_called_once()
            legacy.assert_not_called()
            process.assert_not_called()
            code_only = create.call_args.kwargs
            self.assertIsNone(code_only['execution_contract'])
            self.assertEqual(code_only['code_task']['environment']['mode'], 'current')
            self.assertEqual(code_only['code_task']['new_files'], ['adapter.py'])
            self.assertEqual(code_only['code_task']['validation_command'], ['python', 'benchmark.py'])
            self.assertGreaterEqual(code_only['process_limits']['process_invocations'], 2)
            self.assertGreaterEqual(code_only['process_limits']['process_wall_seconds'], 400 + code_only['code_task']['timeout_sec'])
            self.assertNotIn('approve', app.run.call_args.kwargs)
            formal = strict_formal
            # Reuse this normalized contract for boundaries, not a second runtime/fixture.
            prepared = {**formal, 'code_task': {**execution['code_task'],
                'validation_command': ['python', 'benchmark.py'], 'validation_timeout_sec': 7}}
            arguments = dict(request='Check published conditions', outputs=('experiments',), sources=(),
                root=root, limits={'llm_requests': 4, 'total_tokens': 10000,
                                  'process_invocations': 3, 'process_wall_seconds': 30})
            declared = build_parser().parse_args(['research-session', '--task-kind', 'reproduction',
                '--topic', 'Check published conditions', '--model', 'fixture-model'])
            declared._explicit_resume_destinations = {'cwd'}

            with patch.object(ToolApplication, 'create', side_effect=capture_creation) as create:
                # Verify this consumer invokes the shared validation gate; its
                # type/timeout/independence matrix lives in TaskPlanTests.
                with self.assertRaisesRegex(ValueError, 'validation_command'):
                    migrate_exact_code_creation(declared, execution={**prepared,
                        'code_task': {**prepared['code_task'], 'validation_command': None}}, **arguments)
                with self.assertRaises(ValueError):
                    migrate_exact_code_creation(declared, execution={**prepared,
                        'code_task': {**prepared['code_task'], 'validation_command': prepared['command']}}, **arguments)
                with self.assertRaisesRegex(ValueError, 'cwd must match'):
                    migrate_exact_code_creation(declared, execution={**prepared, 'cwd': str(root.resolve())}, **arguments)
                with self.assertRaisesRegex(ValueError, 'existing absolute directory'):
                    migrate_exact_code_creation(declared, execution={**prepared, 'cwd': str(root / 'missing')}, **arguments)
                for extra in ({'pairs': []}, {'baseline_policy': 'run'}):
                    self.assertFalse(migrate_exact_code_creation(declared, execution={**prepared, **extra}, **arguments))
                with self.assertRaisesRegex(ValueError, 'baseline_policy'):
                    migrate_exact_code_creation(declared, execution={**prepared, 'baseline_policy': 'paired'}, **arguments)
                with self.assertRaises(ValueError):
                    migrate_exact_code_creation(declared, execution={**prepared,
                        'environment': {'mode': 'current'}}, **arguments)
                initial = {**prepared['code_task'], 'workspace_mode': 'copy',
                    'allowed_patterns': ['adapter.py'], 'initial_files': ['adapter.py']}
                for changes in ({'workspace_mode': 'sparse_copy'}, {'kind': 'greenfield'},
                        {'allowed_patterns': ['*.py']},
                        {'allowed_patterns': ['adapter.txt'], 'initial_files': ['adapter.txt']},
                        {'allowed_patterns': ['tests/test_adapter.py'], 'initial_files': ['tests/test_adapter.py']}):
                    with self.subTest(initial_files=changes), self.assertRaises(ValueError):
                        migrate_exact_code_creation(declared, execution={**prepared,
                            'code_task': {**initial, **changes}}, **arguments)
                ordinary = build_parser().parse_args(['research-session', '--task-kind', 'auto',
                    '--topic', 'Check published conditions', '--model', 'fixture-model'])
                no_model = build_parser().parse_args(['research-session', '--task-kind', 'auto',
                    '--topic', 'Topic/paper names do not grant model execution'])
                no_model.model = None
                with self.assertRaisesRegex(ValueError, 'requires a model'):
                    migrate_exact_code_creation(no_model, execution=prepared, **arguments)
                self.assertFalse(migrate_exact_code_creation(ordinary,
                    execution={**prepared, 'baseline_policy': 'reuse'}, **arguments))
                for mode, source in (('empty', None), ('empty', str(project)),
                        ('copy', None), ('sparse_copy', str(root / 'missing'))):
                    with self.subTest(baseline_without_source=(mode, source)):
                        declaration = {**prepared, 'baseline_policy': 'run',
                            'baseline': {'command': prepared['command']},
                            'code_task': {**prepared['code_task'], 'kind': 'greenfield',
                                'workspace_mode': mode, 'code_root': source}}
                        if mode == 'empty':
                            self.assertFalse(migrate_exact_code_creation(ordinary, execution=declaration, **arguments))
                        else:
                            with self.assertRaises((ValueError, RuntimeError)):
                                migrate_exact_code_creation(ordinary, execution=declaration, **arguments)
                create.assert_not_called()
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(migrate_exact_code_creation(ordinary, execution={**prepared,
                        'command': ['python', 'benchmark.py'], 'baseline_policy': 'skip'}, **arguments))
                self.assertEqual([row['source'] for row in create.call_args.kwargs['execution_contract']['conditions']],
                    ['implementation'])
                create.reset_mock()
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(migrate_exact_code_creation(ordinary, execution={**prepared,
                        'command': ['python', 'benchmark.py'], 'baseline_policy': 'run',
                        'baseline': {'command': ['python', 'benchmark.py']},
                        'code_task': {**prepared['code_task'], 'kind': 'greenfield', 'workspace_mode': 'copy'}}, **arguments))
                baseline, candidate = create.call_args.kwargs['execution_contract']['conditions']
                self.assertEqual((baseline['source'], baseline['before_implementation']), ('preparation', True))
                self.assertEqual(candidate['source'], 'implementation')
                self.assertNotIn('before_implementation', candidate)
                self.assertEqual(baseline['execution']['command'], candidate['execution']['command'])
                self.assertNotIn('approve', app.run.call_args.kwargs)
                create.reset_mock()
                data = project / 'observations.tsv'
                data.write_text('value\n1\n')
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(migrate_exact_code_creation(declared, execution={**prepared,
                        'code_task': {**prepared['code_task'], 'workspace_mode': 'copy'}},
                        **{**arguments, 'sources': ({'role': 'dataset', 'locator': str(data)},)}))
                self.assertEqual(create.call_args.kwargs['code_task']['preparation']['data_inputs'], ['observations.tsv'])
                create.reset_mock()
                empty = {**prepared, 'cwd': str(root.resolve()), 'code_task': {**prepared['code_task'],
                    'kind': 'greenfield', 'workspace_mode': 'empty', 'code_root': None}}
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(migrate_exact_code_creation(declared, execution=empty, **arguments))
                create.assert_called_once()
                self.assertEqual(list(create.call_args.kwargs['sources'].values()), [])
                self.assertEqual(create.call_args.kwargs['code_task']['project_path'], '')
                self.assertEqual(measurement_conditions(create.call_args.kwargs['execution_contract'])[0]['execution']['cwd'], str(root.resolve()))

    def test_research_case_uses_checked_in_configs_and_case_local_paths(self) -> None:
        from simple_ar.app.tool_application import ToolApplication
        app = MagicMock()
        app.run.return_value = {'status': 'completed'}
        def capture_creation(root, goal, **options):
            app.state = {'config': options, 'turns': [], 'pending': None}
            return app
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
            checker = case / 'check.py'
            checker.write_text('import math\n', encoding='utf-8')
            (case / "research.toml").write_text(
                '[task]\ngoal = "Improve this project"\noutputs = ["experiments"]\n'
                '[model]\nname = "fixture-model"\n'
                '[execution]\ncode_task_config = "code_task.toml"\n'
                '[execution.environment]\nmode="current"\ntimeout_sec=5\n'
                f'check_command=["python",{json.dumps(str(checker))}]\n',
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
            with (
                patch("simple_ar.cli.main._optional_research_llm_client", return_value=object()),
                patch("simple_ar.app.research_application.create_session") as old,
                patch.object(ToolApplication, "create", side_effect=capture_creation) as create,
                patch("simple_ar.cli.main._render_native_result"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                main(["research-session", "--config", str(case / "research.toml")])

            old.assert_not_called()
            create.assert_called_once()
            app.run.assert_called_once()
            self.assertNotIn('approve', app.run.call_args.kwargs)
            self.assertEqual(app.state['config'], create.call_args.kwargs)
            routed = create.call_args.kwargs
            execution = routed['execution_contract']['conditions'][0]['execution']
            self.assertEqual(Path(execution["cwd"]), project)
            self.assertEqual(Path(execution["command"][0]), Path(sys.executable).absolute())
            self.assertEqual(Path(execution["command"][1]), case / "run.py")
            self.assertEqual(Path(execution["command"][3]), data)
            self.assertIn(case / 'run.py', list(routed['sources'].values()))
            self.assertIn(checker, list(routed['sources'].values()))
            self.assertNotIn(case, list(routed['sources'].values()))  # Explicit file, not its parent or all data directories.
            from simple_ar.tools.workspace import WorkspaceToolGateway
            from simple_ar.tools.implementation import ImplementationToolGateway
            from simple_ar.core.budget import BudgetLedger
            (root / 'native').mkdir()
            workspace = WorkspaceToolGateway(root / 'native', routed['sources'], BudgetLedger({}))
            owner = ImplementationToolGateway(root / 'native', workspace, workspace.ledger,
                'fixture', 'path-binding', exact=routed['code_task'], measurement=routed['execution_contract'])
            bound = owner.approval_binding({}, identity='inspect-paths')
            self.assertIn(str(case / 'run.py'), bound['external_inputs'])
            import hashlib
            self.assertEqual(bound['external_inputs'][str(checker)], hashlib.sha256(checker.read_bytes()).hexdigest())
            with self.assertRaises(ValueError):
                workspace.resolve(str(data))  # Directory access still needs an explicit material/data declaration.

            # CLI paths are relative to the actual cwd; TOML paths already bind
            # to the separate config directory. Same names must not hide a rebase.
            caller = root / 'caller cwd'
            caller.mkdir()
            for name, content in (('paper.md', 'Caller paper bytes'), ('notes.md', 'Caller material bytes')):
                (caller / name).write_text(content, encoding='utf-8')
                (case / name).write_text('Different config-directory bytes: ' + content, encoding='utf-8')
            public = (case / 'research.toml').resolve()
            public.write_text(
                '[task]\ngoal="Check declared conditions"\nkind="auto"\noutputs=["experiments"]\n'
                '[model]\nname="fixture-model"\n[research]\nmaterials_only=true\n'
                '[execution]\ncode_task_config="code_task.toml"\n'
                f'cwd="{project.as_posix()}"\n'
                'timeout_sec=5\nbaseline_policy="skip"\n'
                '[execution.protocol]\nhypothesis="Declared claim"\ndataset="Fixed data"\n'
                'expected_outcome="Compare measured accuracy"\n', encoding='utf-8')
            app.reset_mock()
            with (
                contextlib.chdir(caller),
                patch('simple_ar.cli.main._optional_research_llm_client', return_value=object()),
                patch('simple_ar.app.research_application.create_session') as old,
                patch.object(ToolApplication, 'create', side_effect=capture_creation) as create,
                patch('simple_ar.cli.main._render_native_result'),
                patch('subprocess.run') as process,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                main(['research-session', '--config', str(public),
                      '--local-document', 'paper.md', '--material', 'notes.md'])
            old.assert_not_called()
            process.assert_not_called()
            create.assert_called_once()
            app.run.assert_called_once()
            mounted = create.call_args.kwargs['sources']
            for name in ('paper.md', 'notes.md'):
                self.assertIn(caller / name, mounted.values())
                self.assertNotIn(case / name, mounted.values())
                alias = next(alias for alias, path in mounted.items() if path == caller / name)
                self.assertEqual(Path(mounted[alias]).read_bytes(), (caller / name).read_bytes())
                self.assertNotEqual(Path(mounted[alias]).read_bytes(), (case / name).read_bytes())
                self.assertIn(alias, create.call_args.kwargs['research_policy']['materials'])




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
