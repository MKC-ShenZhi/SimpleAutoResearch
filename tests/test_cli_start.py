from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from simple_ar.cli.parser import build_parser
from simple_ar.cli.research_config import research_defaults
from simple_ar.cli.start import prepare_start
from simple_ar.code_task.runtime.config import load_code_task_init_options, load_code_task_execute_options


class StartTests(unittest.TestCase):
    def test_fixed_protocol_confirmation_retains_queue_and_requires_separate_process_approval(self):
        from simple_ar.cli.main import main
        from simple_ar.app.tool_application import ToolApplication
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / 'measure.py').write_text('from pathlib import Path\n'
                'p=Path("calls.txt")\np.write_text(p.read_text()+"x" if p.exists() else "x")\nprint("score: 3")\n')
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(SystemExit, "status 'paused'"):
                main(['research-session', '--topic', 'Review a literal measurement', '--task-kind', 'measurement',
                    '--outputs', 'experiments',
                    '--interaction', 'checkpoints', '--model', '', '--output-root', str(root / 'out'),
                    '--cwd', str(root), '--primary-metric', 'score', '--timeout-sec', '5',
                    '--process-invocations', '1', '--process-wall-seconds', '10',
                    '--command', sys.executable, 'measure.py'])
            session, = (root / 'out').iterdir()
            app = ToolApplication.load(session)
            self.assertEqual(app.session.manifest.transition_recipe, ToolApplication.RECIPE)
            proposal = app.sources.pending_decision()
            self.assertEqual(proposal['proposal_kind'], 'caller_execution_protocol')
            self.assertFalse(app.session.list_attempts())
            self.assertFalse((root / 'calls.txt').exists())
            queue = json.loads(json.dumps(app.state['turns'][-1]['remaining']))
            requests = list(app.state['requests'])
            reply = {'decision_id': proposal['decision_id'], 'decision_response': 'accept'}
            app = ToolApplication.load(session)
            status = app.run(decision=reply, progress=lambda _: None)
            self.assertEqual(status['status'], 'paused')
            self.assertEqual(app.state['turns'][-1]['remaining'], queue)
            self.assertEqual(app.state['requests'], requests)
            self.assertFalse((root / 'calls.txt').exists())
            self.assertFalse(app.ledger.entries)
            token = app.approval_token(app.state['pending']['call'])
            app = ToolApplication.load(session)
            self.assertEqual(app.run(approved_execution=token, progress=lambda _: None)['status'], 'completed')
            self.assertEqual((root / 'calls.txt').read_text(), 'x')
            self.assertEqual(sum(row.actual.get('process_invocations', 0) for row in app.ledger.entries), 1)
            before = {p.relative_to(session): p.read_bytes() for p in session.rglob('*')
                if p.is_file() and p.name != '.session.lock'}
            app = ToolApplication.load(session)
            self.assertEqual(app.run(decision=reply, progress=lambda _: None)['status'], 'completed')
            self.assertEqual(app.request_execution_confirmation(), proposal)
            self.assertEqual({p.relative_to(session): p.read_bytes() for p in session.rglob('*')
                if p.is_file() and p.name != '.session.lock'}, before)
            self.assertEqual((root / 'calls.txt').read_text(), 'x')

    def test_native_supply_records_only_after_confirmation_without_running_work(self):
        from simple_ar.cli.main import main
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / 'supply.toml'
            config.write_text('supply_id = "add-execution"\nreason = "Measure retained sources"\n'
                '[changes]\nallow_execution = true\n[changes.sources]\nextra = "notes.txt"\n', encoding='utf-8')
            app = MagicMock()
            app.session.store.root = root / 'task'
            args = ['start', '--resume-task', str(root / 'task'), '--supply-task', str(config)]
            with patch('simple_ar.app.tool_application.ToolApplication.load', return_value=app), \
                    patch('simple_ar.cli.main._render_native_result'), patch('sys.stdin.isatty', return_value=False), \
                    contextlib.redirect_stdout(io.StringIO()):
                main(args)
                main([*args, '--prepare-only', '--yes'])
                app.supply.assert_not_called()
                app.run.assert_not_called()
                main([*args, '--yes'])
                app.supply.assert_called_once_with(supply_id='add-execution', reason='Measure retained sources',
                    changes={'allow_execution': True, 'sources': {'extra': str((root / 'notes.txt').resolve())}})
                app.run.assert_not_called()
                config.write_text('supply_id = "replace-execution"\nreason = "Change declared conditions"\n'
                    'replace_existing = true\n[changes]\nallow_execution = true\n', encoding='utf-8')
                main([*args, '--yes'])
                app.supply.assert_called_with(supply_id='replace-execution', reason='Change declared conditions',
                    replace_existing=True, changes={'allow_execution': True})
                app.run.assert_not_called()
                for extra in (['--allow-execution'], ['--retry-model'], ['--approve-execution', 'old'],
                        ['--model', 'profile:other']):
                    with self.subTest(extra=extra), self.assertRaisesRegex(SystemExit, 'separately'):
                        main([*args, *extra, '--yes'])
                with self.assertRaisesRegex(SystemExit, '--resume-task'):
                    main(['start', '--supply-task', str(config), '--yes'])

    def test_native_approval_without_saved_task_stops_before_setup(self):
        from simple_ar.cli.main import main
        with patch("sys.stdin.isatty", return_value=False), \
                patch("simple_ar.cli.start.select_start_model") as selection, \
                patch("simple_ar.app.tool_application.ToolApplication.create") as create:
            for request in ([], ["Explain the supplied notes"]):
                with self.subTest(request=request), self.assertRaisesRegex(SystemExit, "--resume-task"):
                    main(["start", *request, "--approve-execution", "stale-token", "--yes"])
            selection.assert_not_called()
            create.assert_not_called()

    def setUp(self):
        # User-global connections must not change offline CLI fixtures.
        catalog = patch('simple_ar.integrations.model_profiles.load_model_catalog', return_value=None)
        catalog.start()
        self.addCleanup(catalog.stop)

    def test_document_urls_preserve_literal_input_and_require_confirmed_chat_acquisition(self):
        url = 'https://example.test/paper'
        args = build_parser().parse_args(['start', '--kind', 'reproduction', '--goal', 'Inspect paper',
            '--document', url, '--document', 'local-paper.md', '--environment', 'current', '--prepare-only'])
        args._explicit_start_destinations = {'environment'}
        self.assertEqual(args.document, [url, Path('local-paper.md')])
        with patch('sys.stdin.isatty', return_value=False), patch('simple_ar.cli.start.print_line'), \
             patch('simple_ar.research.documents.fulltext.urllib.request.urlopen') as fetch:
            with self.assertRaisesRegex(ValueError, 'Paper URLs require reproduction --chat'):
                prepare_start(args)
        fetch.assert_not_called()

    def test_registered_code_revision_preserves_scope_checker_policy_and_original(self):
        from simple_ar.core.capabilities import CapabilityRegistry
        from simple_ar.core.session import SessionController
        from simple_ar.code_task.orchestration.workflow import initialize_code_task
        from simple_ar.cli.start import reuse_session_materials, session_materials
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            source.mkdir()
            (source / 'main.py').write_text('VALUE = 1\n')
            task = root / 'task.md'
            task.write_text('Fixture project, no execution during preparation.')
            session = SessionController.create(root / 'old', session_id='old', topic='Fixture', registry=CapabilityRegistry())
            initialized = initialize_code_task(run_dir=session.store.root / 'project_run', code_root=source,
                task_file=task, benchmark_command='python main.py',
                edit_scope_allowed_patterns=('main.py',), edit_scope_protected_patterns=('gold/**',))
            session.manifest.state_refs['implementation'] = session.store.write_json('implementation.json',
                {'status': 'validated', 'workspace_dir': str(initialized.workspace_dir), 'code_task_run_dir': str(initialized.run_dir)})
            session.manifest.state_refs['preparation'] = session.store.write_json('prepared.json',
                {'execution': {'cwd': str(initialized.workspace_dir), 'code_task': {'budget_profile': 'large', 'allow_large_edits': True,
                    'edit_budget_overrides': {'max_new_chars': 32000, 'max_files': 6}}}},
                kind='prepared_execution')
            session.save()
            before = session.store.resolve('session_manifest.json').read_bytes()
            session_alias = source / '..' / 'old'
            self.assertEqual(session_materials(session_alias)['code_project'], [initialized.workspace_dir.resolve()])
            args = build_parser().parse_args(['start', '--from-session', str(session_alias), '--reuse', 'code_project',
                '--goal', 'Change VALUE to two', '--output-root', str(root / 'new'), '--prepare-only'])
            reuse_session_materials(args)
            self.assertEqual(args.allow, ['main.py'])
            self.assertEqual(args.validate, 'python main.py')
            with patch('sys.stdin.isatty', return_value=False), patch('simple_ar.cli.start.print_line'), \
                 patch('simple_ar.cli.main._run_native_start') as native:
                self.assertIsNone(prepare_start(args))
            native.assert_called_once_with(args)
            self.assertIn('gold/**', args._code_task['protected_patterns'])
            self.assertEqual(args._code_task['allowed_patterns'], ['main.py'])
            self.assertEqual(args._code_task['validation_command'], ['python', 'main.py'])
            self.assertEqual(args._code_task['edit_policy']['budget_profile'], 'large')
            self.assertTrue(args._code_task['edit_policy']['allow_large_edits'])
            self.assertEqual(args._code_task['edit_policy']['edit_budget_overrides'], {'max_new_chars': 32000, 'max_files': 6})
            self.assertEqual((initialized.workspace_dir / 'main.py').read_text(), 'VALUE = 1\n')
            self.assertEqual(session.store.resolve('session_manifest.json').read_bytes(), before)
            self.assertNotIn('record each figure', args._code_task['goal'])
            self.assertFalse(list((root / 'new').rglob('*.toml')))
            delivery = session.store.write_json('analysis/analysis.json', {'schema_version': 'code_analysis.v1'})
            implementation = session.store.read_json(session.manifest.state_refs['implementation'])
            implementation['artifact_refs'] = {'code_analysis': {'path': delivery.path}}
            session.manifest.state_refs['implementation'] = session.store.write_json('implementation.json', implementation)
            session.save()
            revised = build_parser().parse_args(['start', '--kind', 'figure', '--from-session', str(session.store.root),
                '--reuse', 'code_project', '--goal', 'Explain my existing figure',
                '--output-root', str(root / 'figure-revision'), '--prepare-only'])
            with patch('sys.stdin.isatty', return_value=False), patch('simple_ar.cli.main._run_native_start') as native:
                self.assertIsNone(prepare_start(revised))
            native.assert_called_once_with(revised)
            self.assertIn('"path": "outputs/<filename>"', revised._code_task['goal'])
            self.assertIn('Explain my existing figure', revised._code_task['goal'])
            conflict = build_parser().parse_args(['start', '--from-session', str(session.store.root), '--reuse', 'code_project', '--allow', '**'])
            with self.assertRaisesRegex(ValueError, 'do not override'):
                reuse_session_materials(conflict)
            # Repeating measurement uses the saved protocol and existing files;
            # creation-only initial_files must never enter the new configuration.
            session.manifest.state_refs['preparation'] = session.store.write_json('prepared.json',
                {'execution': {'cwd': str(initialized.workspace_dir), 'command': ['python', 'main.py', '--measure'],
                    'result_schema': {'required_metrics': ['count'], 'primary_metric': 'count',
                                      'output_files': {'counts': 'counts.json'},
                                      'metric_sources': {'count': {'output': 'counts', 'path': ['count']}}}, 'timeout_sec': 45,
                    'protocol': {'hypothesis': 'Check published count', 'dataset': 'Fixture data',
                                 'expected_outcome': 'Compare the recorded count'},
                    'code_task': {'budget_profile': 'large', 'allow_large_edits': True}}},
                kind='prepared_execution')
            session.save()
            retained = session.store.resolve('session_manifest.json').read_bytes()
            paper = root / 'paper.md'
            paper.write_text('Supplied paper and fixed comparison scope.')
            repeat = build_parser().parse_args(['start', '--kind', 'reproduction', '--from-session',
                str(session.store.root), '--reuse', 'code_project', '--goal', 'Check readiness and repeat the fixed measurement',
                '--document', str(paper), '--output-root', str(root / 'repeat'), '--prepare-only', '--yes'])
            with patch('sys.stdin.isatty', return_value=False), patch('simple_ar.cli.start.print_line'), patch('subprocess.run') as process:
                repeated = prepare_start(repeat)
            process.assert_not_called()
            parsed = tomllib.loads(repeated.read_text())
            self.assertNotIn('initial_files', parsed['execution'])
            self.assertEqual(parsed['execution']['command'][1:], ['main.py', '--measure'])
            self.assertEqual(parsed['execution']['protocol']['dataset'], 'Fixture data')
            self.assertEqual(parsed['execution']['timeout_sec'], 45)
            self.assertEqual(parsed['execution']['metric_sources'], {'count': {'output': 'counts', 'path': ['count']}})
            repeat_code = tomllib.loads((repeated.parent / 'code_task.toml').read_text())
            self.assertEqual(parsed['execution']['command'][0], repeat_code['environment']['python'])
            self.assertEqual(repeat_code['edit_scope']['allowed_patterns'], ['main.py'])
            self.assertIn('gold/**', repeat_code['edit_scope']['protected_patterns'])
            self.assertEqual(repeat_code['benchmark']['command'], 'python main.py')
            self.assertEqual(session.store.resolve('session_manifest.json').read_bytes(), retained)

    def test_ordinary_preferences_enter_one_native_owner_with_declarations_and_read_only_mounts(self):
        from unittest.mock import MagicMock
        from simple_ar.cli.main import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "measurements.csv"
            data.write_text("method,cost,quality\na,1,3\nb,2,4\n", encoding="utf-8")
            notes = root / "notes.md"
            notes.write_text("Supplied semantics, not independent observations.", encoding="utf-8")
            paper = root / "paper.md"
            paper.write_text("Original source fixture.", encoding="utf-8")
            project = root / "project"
            project.mkdir()
            checker = root / "checker.py"
            checker.write_text("raise SystemExit('user evaluator')\n", encoding="utf-8")
            interpreter = root / "project python"
            interpreter.write_bytes(b"interpreter entry fixture")
            goal = "  Compare costs.\nKeep the uncertainty branch.  "
            originals = {path: path.read_bytes() for path in (data, notes, checker)}
            for kind in ("survey", "writing", "reproduction", "bug_fix", "data_analysis", "figure", "code"):
                app = MagicMock()
                app.session.store.root = root / kind
                from simple_ar.core.budget import BudgetLedger
                app.ledger = BudgetLedger({})
                app.state = {"pending": None}
                app.workspace.workspace = root / kind / "workspace"
                app.session.store.exists.return_value = False
                options = ["start", "--kind", kind, "--goal", goal, "--model", "profile:chosen",
                    "--source", str(checker), "--document", str(paper), "--material", str(notes),
                    "--project-python", str(interpreter), "--max-turns", "7", "--max-tokens", "43210",
                    "--output-root", str(root / "runs"), "--prepare-only"]
                if kind != "code":
                    options += ["--data-file", str(data), "--observation-unit", "one supplied summary",
                        "--data-attribution", "User's dataset v2", "--data-mode", "values"]
                if kind == "data_analysis":
                    options += ["--with-report", "--allow-execution"]
                if kind in {"bug_fix", "reproduction", "code"}:
                    options += ["--project", str(project), "--data-path", str(data)]
                with self.subTest(kind=kind), patch("sys.stdin.isatty", return_value=False), \
                        patch("simple_ar.cli.start.select_start_model") as selection, \
                        patch("simple_ar.app.tool_application.ToolApplication.create", return_value=app) as create, \
                        patch("simple_ar.cli.start.new_research_session_root", side_effect=AssertionError("legacy configuration")), \
                        patch("subprocess.run") as process, contextlib.redirect_stdout(io.StringIO()):
                    main(options)
                selection.assert_called_once()
                app.run.assert_not_called()
                process.assert_not_called()
                self.assertTrue(create.call_args.args[1].startswith(goal + "\n\n"))
                kwargs = create.call_args.kwargs
                self.assertEqual(kwargs["model"], "profile:chosen")
                self.assertEqual(kwargs["max_turns"], 7)
                self.assertEqual(kwargs["max_tokens"], 43210)
                self.assertEqual(kwargs["project_python"], interpreter)
                self.assertEqual(kwargs["allow_execution"], kind == "data_analysis")
                self.assertFalse(kwargs["allow_network"])
                expected_sources = [checker.resolve(), paper.resolve(), notes.resolve()]
                if kind in {"bug_fix", "reproduction", "code"}:
                    expected_sources.extend([data.resolve(), project.resolve()])
                elif kind != "code":
                    expected_sources.append(data.resolve())
                self.assertEqual(list(kwargs["sources"].values()), expected_sources)
                self.assertEqual(list(kwargs["sources"].values()).count(notes.resolve()), 1)
                self.assertIn("document", create.call_args.args[1])
                self.assertIn("material", create.call_args.args[1])
                self.assertIn("outcome preference (not an access grant)", create.call_args.args[1])
                if kind in {"bug_fix", "reproduction", "code"}:
                    self.assertIn(project.resolve(), kwargs["sources"].values())
                    self.assertIn('"project"', create.call_args.args[1])
                if kind != "code":
                    self.assertIn("one supplied summary", create.call_args.args[1])
                    self.assertIn("User's dataset v2", create.call_args.args[1])
                    self.assertIn('"data_mode": "values"', create.call_args.args[1])
                self.assertFalse(list(root.rglob("code_task.toml")))
                self.assertFalse(list(root.rglob("verify_delivery.py")))
            self.assertEqual({path: path.read_bytes() for path in originals}, originals)
            app.run.return_value = {"status": "paused", "status_reason": "Awaiting command approval"}
            with patch("sys.stdin.isatty", return_value=False), \
                    patch("simple_ar.cli.start.select_start_model"), \
                    patch("simple_ar.app.tool_application.ToolApplication.create", return_value=app) as create, \
                    patch("simple_ar.cli.start.new_research_session_root", side_effect=AssertionError("legacy configuration")), \
                    patch("subprocess.run") as process, contextlib.redirect_stdout(io.StringIO()):
                main(["start", "--kind", "data_analysis", "--scripted", "--goal", goal,
                      "--data-file", str(data), "--allow-execution", "--yes"])
            self.assertTrue(create.call_args.kwargs["allow_execution"])
            app.run.assert_called_once()
            self.assertEqual(app.run.call_args.kwargs["approved_execution"], "")
            self.assertIsNone(app.run.call_args.kwargs["approve"])
            process.assert_not_called()  # Summary consent never executes a command.
            # The explicit menu is an input preference; it enters the same owner.
            from simple_ar.cli.start import FUNCTION_LABELS
            for number, kind in enumerate(FUNCTION_LABELS, 1):
                with self.subTest(guided=kind), patch("sys.stdin.isatty", return_value=True), \
                        patch("builtins.input", side_effect=[str(number), str(notes), ""]) as prompts, \
                        patch("simple_ar.cli.main._run_native_start") as native:
                    main(["start", "--guided", "--goal", goal, "--max-turns", "7", "--prepare-only"])
                adapted = native.call_args.args[0]
                self.assertEqual(adapted.kind, kind)
                self.assertTrue(adapted.request.startswith(goal + "\n\n"))
                self.assertFalse(adapted.allow_execution)
                self.assertFalse(adapted.allow_network)
                self.assertEqual(adapted.source, [notes.resolve()])
                self.assertEqual(adapted.max_turns, 7)
                self.assertEqual(prompts.call_count, 3)  # Preference + common paths, no technical survey.
                args = build_parser().parse_args(["start", "--kind", kind, "--goal", goal,
                                                 "--source", str(notes), "--prepare-only"])
                with patch("sys.stdin.isatty", return_value=False), \
                        patch("simple_ar.cli.main._run_native_start") as native:
                    self.assertIsNone(prepare_start(args))
                self.assertTrue(native.call_args.args[0].request.startswith(goal + "\n\n"))
            # Bare start already owns the native default; preferences do not change it.
            for argv in (["start"], ["start", goal], ["start", "--goal", goal]):
                with patch("simple_ar.cli.main._run_native_start") as native, \
                        patch("builtins.input", side_effect=AssertionError("no menu")):
                    main(argv)
                native.assert_called_once()
                self.assertIsNone(native.call_args.args[0].kind)

    def test_creation_rejects_unmapped_controls_and_unsafe_data_before_calls(self):
        from simple_ar.cli.main import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "measurements.csv"
            data.write_text("x,y\n1,2\n", encoding="utf-8")
            secret = root / ".env"
            secret.write_text("private fixture", encoding="utf-8")
            large = root / "large.csv"
            with large.open("wb") as stream:
                stream.truncate(20 * 1024 * 1024 + 1)
            base = ["start", "--kind", "data_analysis", "--scripted", "--goal", "Analyze supplied values",
                    "--data-file", str(data), "--prepare-only", "--output-root", str(root / "runs")]
            cases = [
                ([*base, "--timeout-sec", "60"], "timeout_sec"),
                ([*base, "--interaction", "checkpoints"], "interaction"),
                ([*base, "--data-max-points", "10000"], "data_max_points"),
                ([*base, "--data-max-mb", "20"], "data_max_mb"),
                ([*base, "--validate", "python check.py"], "validate"),
                ([*base, "--allow", "**"], "allow"),
                ([*base, "--chat"], "chat"),
                ([*base, "--template", "experiment"], "template"),
                ([*base, "--document", "https://example.test/paper"], "local mounted"),
                ([*base, "--source", str(secret)], "Private/runtime"),
                (["start", "Analyze supplied material", "--source", str(secret), "--prepare-only"], "Private/runtime"),
                ([*base, "--data-file", str(large)], "20 MiB"),
                ([*base, "--resume-task", str(root)], "saved task"),
                ([*base, "--approve-execution", "old-command"], "requires --resume-task"),
            ]
            with patch("simple_ar.cli.start.select_start_model") as model, \
                    patch("simple_ar.app.tool_application.ToolApplication.create") as create, \
                    patch("simple_ar.cli.start.new_research_session_root") as legacy:
                for argv, message in cases:
                    with self.subTest(argv=argv), self.assertRaisesRegex(SystemExit, message):
                        main(argv)
                model.assert_not_called()
                create.assert_not_called()
                legacy.assert_not_called()
            self.assertFalse((root / "runs").exists())
            # Compatibility is selected by explicit controls, including values
            # equal to parser defaults, rather than a task-name allowlist.
            from simple_ar.cli.main import _explicit_session_option_destinations
            from simple_ar.cli.start import migrate_new_start
            controls = (["--allow", "src/**"], ["--validate", "python check.py"],
                        ["--template", "material_report"], ["--sources", "materials"], ["--fulltext"],
                        ["--web-extract-backend", "direct"], ["--max-cited-sources", "3"],
                        ["--interaction", "checkpoints"], ["--environment", "current"],
                        ["--hypothesis", "Claim"], ["--dataset", "Conditions"],
                        ["--expected-outcome", "Criterion"], ["--metric", "score"],
                        ["--output-files", "{}"], ["--metric-sources", "{}"],
                        ["--cwd", str(root)], ["--timeout-sec", "300"],
                        ["--requirements", "requirements.txt"], ["--install-project"],
                        ["--check-argv", '["python", "check.py"]'], ["--command", "python", "run.py"])
            # Each explicit control owns one routing check. Ordinary preferences
            # already cover every kind; exact code controls have their own route.
            for flags in controls:
                argv = ["start", "--kind", "survey", "--goal", "Keep exact controls", *flags]
                parser = build_parser()
                args = parser.parse_args(argv)
                args._explicit_start_destinations = _explicit_session_option_destinations(parser, argv, set(), command="start")
                before = vars(args).copy()
                with self.subTest(controls=flags):
                    self.assertFalse(migrate_new_start(args))
                    self.assertEqual(vars(args), before)
            for flags in (["--allow", "src/**"], ["--validate", "python check.py"]):
                args = build_parser().parse_args(["start", "--kind", "bug_fix", "--goal", "Keep exact controls", *flags])
                with self.subTest(incomplete_code=flags), self.assertRaisesRegex(
                        ValueError, 'requires --project, --allow and --validate'):
                    migrate_new_start(args)
            for flags in (["--chat"], ["--resume-setup", str(root)],
                          ["--from-session", str(root), "--reuse", "report"],
                          ["--data-file", str(data), "--value-column", "x"],
                          ["--data-file", str(data), "--x-column", "x"],
                          ["--data-file", str(data), "--paired-baseline", "x"]):
                args = build_parser().parse_args(["start", "--kind", "data_analysis", *flags])
                self.assertFalse(migrate_new_start(args))

    def test_saved_delivery_reuse_keeps_draft_evidence_and_old_session(self):
        from simple_ar.core.capabilities import CapabilityRegistry
        from simple_ar.core.session import SessionController
        from simple_ar.cli.start import session_materials
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = SessionController.create(root / "original", session_id="original",
                topic="Measured comparison", registry=CapabilityRegistry())
            ref = session.store.write_json("delivery/report.json", {"status": "completed"})
            session.store.write_text("delivery/report_body.md", "# Comparison\nA draft.")
            evidence = session.store.write_json("delivery/experiment_evidence.json", {
                "schema_version": "report_experiment_evidence.v1", "measured": True})
            session.store.write_json("delivery/citation_map.json", {"schema_version": "citation_map.v1", "entries": []})
            session.manifest.state_refs["report"] = ref
            documents = session.store.write_json("sources/document_bundle.json", {
                "schema_version": "document_bundle.v1", "documents": [], "chunks": []})
            session.manifest.state_refs["documents"] = documents
            current_documents = session.store.write_json("sources/after-reading.json", {
                "schema_version": "document_bundle.v1", "documents": [{"document_id": "retained-source",
                    "title": "Original source", "source": "local_files", "abstract": "Source text, not draft judgment."},
                    {"document_id": "legacy-note", "title": "Untyped historical material", "source": "local_files",
                     "source_id": str(root / 'old-material.txt'), "abstract": "Old derived judgment."}], "chunks": []},
                kind="document_bundle", schema="document_bundle.v1")
            session.manifest.state_refs['assets'] = session.store.write_json('inputs/assets.json',
                {'assets': [{'role': 'material', 'locator': str(root / 'old-material.txt')}]})
            session.store.write_json("writing/report_inputs.json", {"sources": [current_documents.to_dict()]})
            session.manifest.state_refs["writer"] = session.store.write_json("writing/writer.json", {
                "input_snapshot": {"path": "report_inputs.json"}})
            code = session.store.write_json("implementation/code_analysis/analysis.json", {"schema_version": "code_analysis.v1"})
            implementation = session.store.write_json("implementation/implementation.json", {
                "status": "validated", "artifact_refs": {"code_analysis": {"path": "code_analysis/analysis.json"}},
                "workspace_dir": str(root / "retired/code_task/workspace"),
                "code_task_run_dir": str(root / "retired")})
            session.manifest.state_refs["implementation"] = implementation
            session.save()
            original = (session.store.root / "session_manifest.json").read_bytes()
            files = session_materials(session.store.root)["report"]
            self.assertEqual(session_materials(session.store.root)["code_analysis"], [session.store.resolve(code)])
            self.assertNotIn("code_project", session_materials(session.store.root))
            with self.assertRaisesRegex(ValueError, "No current reusable code_project"):
                self.prepare("--from-session", str(session.store.root), "--reuse", "code_project",
                    "--goal", "Revise the retired project", "--prepare-only")
            self.assertEqual(len(files), 4)
            self.assertIn(session.store.resolve(evidence).resolve(), files)
            self.assertIn(session.store.resolve(current_documents).resolve(), files)
            self.assertNotIn(session.store.resolve(documents).resolve(), files)
            # Ordinary result reuse enters the same native task owner, with
            # complete result packages mounted read-only. It never creates a
            # second structured report plan or changes the original session.
            native_args = build_parser().parse_args(["start", "--from-session", str(session.store.root),
                "--reuse", "report", "--goal", "Reorganize from the existing results", "--prepare-only"])
            with patch("sys.stdin.isatty", return_value=False), \
                 patch("simple_ar.cli.main._run_native_start") as native:
                self.assertIsNone(prepare_start(native_args))
            native.assert_called_once_with(native_args)
            self.assertIn("Prior prose is editable material, not primary evidence", native_args.request)
            self.assertIn(str(files[0]), native_args.request)
            self.assertIn(files[0].parent, native_args.source)
            self.assertIn(session.store.resolve(current_documents).resolve(), native_args.source)
            self.assertEqual((session.store.root / "session_manifest.json").read_bytes(), original)
            self.assertFalse(list(root.glob("new/**/research.toml")))
            config = self.prepare("--from-session", str(session.store.root), "--reuse", "report",
                "--goal", "Reorganize the argument and replace the chapter structure", "--template", "material_report", "--output-root", str(root / "new"), "--prepare-only")
            defaults = research_defaults(["research-session", "--config", str(config)])
            self.assertEqual(defaults["task_kind"], "writing")
            self.assertEqual(defaults["report_outline_strategy"], "adaptive")
            self.assertEqual(defaults["report_draft_scope"], "document")
            self.assertIn("Reorganize the argument", config.read_text())
            selected = set(defaults["material"])
            self.assertEqual(selected - {str(path) for path in files}, {str(config.parent / "reused-draft-1.json")})
            self.assertNotIn(str(files[0]), selected)
            draft = json.loads((config.parent / "reused-draft-1.json").read_text())
            self.assertEqual(draft['documents'][0]['metadata']['kind'], 'prior_draft')
            self.assertEqual(draft['documents'][0]['metadata']['evidence_role'], 'prior_draft_not_primary_evidence')
            followup = self.prepare("--kind", "survey", "--from-session", str(session.store.root),
                "--reuse", "report", "--goal", "Investigate a new question using relevant original sources",
                "--output-root", str(root / "followup"), "--prepare-only")
            followup_defaults = research_defaults(["research-session", "--config", str(followup)])
            self.assertEqual(followup_defaults["task_kind"], "survey")
            self.assertFalse(tomllib.loads(followup.read_text())["research"]["materials_only"])
            self.assertEqual(followup_defaults["material"], [str(followup.parent / 'reused-sources-1.json')])
            filtered = json.loads((followup.parent / 'reused-sources-1.json').read_text())
            self.assertEqual([r['document_id'] for r in filtered['documents']], ['retained-source'])
            self.assertEqual(len(session.store.read_json(current_documents)['documents']), 2)
            self.assertFalse(list(followup.parent.glob("reused-draft-*.json")))
            from simple_ar.cli.intake_dialogue import _arguments
            from argparse import Namespace
            self.assertEqual(_arguments(Namespace(_reuse_report_drafts=[str(files[0])]))['_reuse_report_drafts'], [str(files[0])])
            self.assertEqual((session.store.root / "session_manifest.json").read_bytes(), original)
            with self.assertRaisesRegex(ValueError, "No current reusable"):
                self.prepare("--from-session", str(session.store.root), "--reuse", "data_analysis",
                    "--goal", "Explain", "--prepare-only")
            # Native deliveries use the same mounted path rule, but resume the
            # saved tool task rather than manufacturing a legacy writing task.
            from simple_ar.app.tool_application import ToolApplication
            from simple_ar.cli.main import main
            app = ToolApplication.create(root / "native", "Reuse results", model=None, sources={},
                max_turns=0, max_tokens=0, max_attempts=4)
            native = app.session
            native.store.write_text("workspace/note.md", "Saved results, not new measurements.")
            app.run(actions=[{'tool_name': 'deliver_result', 'arguments': {
                'summary': 'A short note', 'artifacts': ['workspace/note.md'],
                'unresolved': ['Raw data unavailable']}}])
            self.assertEqual(session_materials(native.store.root), {"artifacts": [native.store.root / "workspace/note.md"]})
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                main(["results", str(native.store.root)])
            self.assertIn("--resume-task", output.getvalue())
            self.assertIn("Raw data unavailable", output.getvalue())
            self.assertNotIn("--kind writing", output.getvalue())
            native.manifest.status = "paused"
            native.save()
            self.assertEqual(session_materials(native.store.root), {})
            native.manifest.status = "partial"
            native.save()
            for invalid in ("workspace/../session_manifest.json", "workspace/.env", "source0/note.md", "workspace/missing.md"):
                native.store.write_json("delivery.json", {"summary": "Invalid", "artifacts": [invalid], "unresolved": []})
                with self.assertRaises(ValueError):
                    session_materials(native.store.root)

    def test_table_options_share_owner_defaults_and_choices(self):
        from simple_ar.cli.research_config import FIELDS, data_options_supplied, setup_option_contract
        from simple_ar.result_analysis.table import TABLE_CHOICES, TableSpec
        parser = build_parser()
        args = parser.parse_args(["start", "--kind", "survey"])
        self.assertFalse(data_options_supplied(args))
        for name, default in TableSpec.defaults().items():
            destination = FIELDS["analysis"][name][0]
            self.assertEqual(getattr(args, destination), default)
        contract = setup_option_contract('data_analysis')
        for name, choices in TABLE_CHOICES.items():
            destination = FIELDS["analysis"][name][0]
            if destination in contract:
                self.assertEqual(set(contract[destination]['allowed_values']), set(choices))
        for flag, value in (("--data-mode", "values"), ("--data-max-mb", "0"),
                            ("--data-association", "pearson"), ("--group-column", "group")):
            with self.subTest(flag=flag):
                self.assertTrue(data_options_supplied(parser.parse_args(["start", flag, value])))

    def test_explicit_survey_protocol_plans_document_without_changing_existing_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.md'
            source.write_text('A supplied source, not a measured experiment.')
            config = self.prepare('--kind', 'survey', '--goal', 'Compare supplied evidence',
                '--document', str(source), '--sources', 'materials', '--output-root', str(root / 'runs'), '--prepare-only')
            defaults = research_defaults(['research-session', '--config', str(config)])
            self.assertEqual(defaults['report_outline_strategy'], 'adaptive')
            self.assertTrue(defaults['report_document_review'])
            self.assertEqual(defaults['report_draft_scope'], 'document')
            self.assertEqual(defaults['report_review_scope'], 'document')
            self.assertNotIn('command_argv', defaults)
            old = root / 'old.toml'
            old.write_text('[task]\ngoal="Review"\nkind="survey"\noutputs=["report"]\n[report]\noutline_strategy="template"\n')
            old_bytes = old.read_bytes()
            self.assertEqual(research_defaults(['research-session', '--config', str(old)])['report_outline_strategy'], 'template')
            self.assertEqual(old.read_bytes(), old_bytes)

    def test_reproduction_data_paths_use_common_configuration_and_cwd_preparation(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = root / 'paper.md'
            paper.write_text('A supplied conclusion.')
            data = root / 'data'
            data.mkdir()
            notes = root / 'previous-observations.json'
            notes.write_text('{"status":"partial","scope_complete":false}')
            common = ['--kind', 'reproduction', '--goal', 'Check conclusion', '--document', str(paper),
                      '--material', str(notes),
                      '--cwd', str(root), '--data-path', str(data), '--data-path', str(data),
                      '--hypothesis', 'Declared claim', '--dataset', 'Explicit directory; split unspecified',
                      '--expected-outcome', 'Compare score', '--metric', 'score',
                      '--output-root', str(root / 'runs'), '--prepare-only']
            config = self.prepare(*common, '--command', 'python', 'run.py')
            defaults = research_defaults(['research-session', '--config', str(config)])
            self.assertEqual(defaults['data_path'], [str(data.resolve())])
            self.assertEqual(defaults['material'], [str(notes.resolve())])
            self.assertEqual(defaults['command_argv'], ['python', 'run.py'])
            facts = json.loads((config.parent / 'preparation.json').read_text())
            self.assertEqual(facts['data_paths'][0]['kind'], 'directory')
            self.assertIn('size does not establish', ' '.join(facts['limitations']))
            with self.assertRaisesRegex(ValueError, 'not found'):
                self.prepare(*common, '--data-path', str(root / 'absent'), '--command', 'python', 'run.py')
            with self.assertRaisesRegex(ValueError, 'requires --kind reproduction'):
                self.prepare('--kind', 'survey', '--goal', 'Survey', '--sources', 'materials', '--data-path', str(data), '--prepare-only')

    def test_explicit_writing_templates_match_material_not_failed_experiment(self):
        from simple_ar.report.schema import ReportRuntimeConfig
        from simple_ar.report.templates import load_report_template_bundle
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = root / "notes.md"
            material.write_text("# Notes\nSupplied observations, not a locally executed experiment.\n", encoding="utf-8")
            config = self.prepare("--kind", "writing", "--goal", "Explain notes", "--material", str(material),
                                  "--template", "material_report", "--output-root", str(root / "runs"), "--prepare-only")
            defaults = research_defaults(["research-session", "--config", str(config)])
            self.assertEqual(defaults["report_template"], "material_report")
            self.assertEqual(defaults["report_draft_scope"], "document")
            self.assertEqual(defaults["report_review_scope"], "document")
            for genre in ("analysis_report", "source_review"):
                generated = self.prepare("--kind", "writing", "--goal", "Explain notes",
                    "--material", str(material), "--template", genre,
                    "--output-root", str(root / genre), "--prepare-only")
                settings = tomllib.loads(generated.read_text())["report"]
                self.assertEqual(settings["draft_scope"], "document")
                self.assertEqual(settings["review_scope"], "document")
            self.assertEqual(defaults["report_outline_strategy"], "adaptive")
            # Keep alias/content checks until a domain test owns these exact
            # builtin defaults; explicit analysis_report resolution is covered
            # by ResearchApplicationTests.test_writing_auto_template_keeps_started_snapshot_across_default_change.
            for name in ("", "auto", "material_report"):
                bundle = load_report_template_bundle(report_mode="supplied_materials", config=ReportRuntimeConfig(template=name))
                self.assertEqual(bundle.name, "material_report")
                self.assertIn("## Findings", bundle.template_markdown)
                self.assertTrue(bundle.criteria_markdown.strip())
            self.assertEqual(load_report_template_bundle(report_mode="experiment",
                config=ReportRuntimeConfig(template="auto")).name, "experiment")

    def test_data_configuration_and_resume_hint_do_not_require_a_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data.csv"
            source.write_text("value\n1\n2\n", encoding="utf-8")
            args = build_parser().parse_args(["start", "--kind", "data_analysis", "--goal", "Describe",
                "--data-file", str(source), "--value-column", "value", "--observation-unit", "one row",
                "--model", "unused-model", "--output-root", str(root / "runs"), "--prepare-only"])
            with patch("sys.stdin.isatty", return_value=False), patch("simple_ar.cli.start.print_line") as output:
                config = prepare_start(args)
            self.assertEqual(research_defaults(["research-session", "--config", str(config)])["model"], "")
            resume_hint = next(call.args[0] for call in output.call_args_list if "resume command printed by the task owner" in call.args[0])
            self.assertNotIn("--model", resume_hint)
            self.assertNotIn("--session-root", resume_hint)  # Setup does not select the eventual task recipe.

    def test_explicit_custom_writing_template_keeps_its_topology(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = root / "notes.md"
            material.write_text("Supplied notes", encoding="utf-8")
            custom = root / "material_report.md"
            custom.write_text("# Custom\n## Findings\nUse supplied notes.\n## Conclusion\nExplain scope.", encoding="utf-8")
            config = self.prepare("--kind", "writing", "--goal", "Explain", "--material", str(material),
                                  "--template", str(custom), "--output-root", str(root / "runs"), "--prepare-only")
            settings = tomllib.loads(config.read_text(encoding="utf-8"))["report"]
            self.assertEqual(settings["template"], str(custom.resolve()))
            self.assertNotIn("outline_strategy", settings)
            self.assertNotIn("draft_scope", settings)
            self.assertNotIn("review_scope", settings)

    def test_setup_preserves_selected_model_and_catalog_default(self):
        from simple_ar.integrations.model_profiles import ModelCatalog
        with tempfile.TemporaryDirectory() as directory:
            for model in ("env", "custom-model"):
                with self.subTest(model=model):
                    args = build_parser().parse_args(["start", "--kind", "survey", "--goal", "Review",
                        "--sources", "search", "--model", model, "--output-root", directory, "--prepare-only"])
                    with patch("sys.stdin.isatty", return_value=False), patch("simple_ar.cli.start.print_line") as output:
                        config = prepare_start(args)
                    self.assertEqual(research_defaults(["research-session", "--config", str(config)])["model"], model)
            catalog = ModelCatalog.model_validate({"profiles": {
                name: {"api": "openai_chat", "base_url": "https://example.test/v1", "model": name,
                       "api_key_env": "TEST_ABSENT_KEY", "capabilities": ["text"], "stream": True,
                       "request_timeout_sec": 600.0}
                for name in ("first", "second")}, "routes": {"default": "first"}})
            for explicit, reply, expected in (("env", "2", "second"), ("profile:first", None, "first")):
                args = build_parser().parse_args(["start", "--kind", "survey", "--goal", "Compare [methods]",
                    "--sources", "search", "--model", explicit, "--output-root", directory, "--prepare-only"])
                with patch('simple_ar.integrations.model_profiles.load_model_catalog', return_value=catalog), \
                     patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value=reply) as prompt, \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    config = prepare_start(args)
                self.assertEqual(research_defaults(['research-session', '--config', str(config)])['model'], f'profile:{expected}')
                self.assertIn('600s', output.getvalue())
                self.assertIn('Compare [methods]', output.getvalue())
                self.assertEqual(prompt.call_count, 1 if reply is not None else 0)

    def test_data_setup_reports_columns_and_rejects_bad_shape_before_saving(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data.csv"
            output = root / "runs"
            args = ["--kind", "data_analysis", "--goal", "Describe values",
                    "--data-file", str(source), "--value-column", "loss",
                    "--observation-unit", "one run", "--output-root", str(output), "--prepare-only"]
            source.write_text("value,group\n0.2,A\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Available columns.*value.*group"):
                self.prepare(*args)
            self.assertFalse(output.exists())
            # Ragged-row permutations belong to TableAnalysisTests. Keep the
            # entrance's diagnostic and pre-save rejection for columns/bytes.
            source.write_bytes(b"loss\n\xff\n")
            with self.assertRaisesRegex(ValueError, "UTF-8"):
                self.prepare(*args)
            self.assertFalse(output.exists())

    def test_data_prepare_does_not_measure_or_infer_numeric_column_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data.csv"
            source.write_text("loss\nnot-a-number\n", encoding="utf-8")
            config = self.prepare("--kind", "data_analysis", "--goal", "Describe",
                                  "--data-file", str(source), "--value-column", "loss",
                                  "--observation-unit", "one row", "--output-root", str(root / "runs"),
                                  "--prepare-only")
            self.assertTrue(config.is_file())
            self.assertEqual(research_defaults(["research-session", "--config", str(config)])["value_column"], ["loss"])

    def test_shared_coordinate_start_roundtrips_explicit_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "paired.csv"
            source.write_text("step,A,B\n0,1,2\n1,2,3\n", encoding="utf-8")
            config = self.prepare("--kind", "data_analysis", "--goal", "Compare supplied coordinates",
                                  "--data-file", str(source), "--value-column", "A", "--value-column", "B",
                                  "--observation-unit", "one paired point", "--data-mode", "values",
                                  "--data-plot", "scatter", "--x-column", "step", "--value-unit", "points",
                                  "--series-layout", "shared", "--output-root", str(root / "runs"), "--prepare-only")
            defaults = research_defaults(["research-session", "--config", str(config)])
            self.assertEqual(defaults["series_layout"], "shared")
            self.assertEqual(defaults["value_column"], ["A", "B"])
            self.assertEqual(defaults["data_plot"], "scatter")

    def test_writing_accepts_parser_supported_html_in_both_material_roles(self):
        from simple_ar.cli.research_config import validate_session_arguments
        from simple_ar.research.documents.extractors import LocalDocumentParser
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for role in ("--document", "--material"):
                for suffix in (".html", ".htm", ".HTML"):
                    with self.subTest(role=role, suffix=suffix):
                        material = root / ("source" + suffix)
                        text = "<html><body><h1>Published source</h1><p>Original conditions remain readable.</p></body></html>"
                        material.write_text(text, encoding="utf-8")
                        config = self.prepare("--kind", "writing", "--goal", "Compare the supplied sources",
                            role, str(material), "--template", "material_report", "--output-root", str(root / "runs"), "--prepare-only")
                        values = research_defaults(["research-session", "--config", str(config)])
                        self.assertEqual(values["outputs"], ["report"])
                        self.assertEqual(values["local_document" if role == "--document" else "material"], [str(material.resolve())])
                        args = build_parser(research_defaults=values).parse_args(["research-session", "--config", str(config)])
                        validate_session_arguments(args)
                        self.assertIn("Original conditions remain readable", LocalDocumentParser().parse(material).text)
                        self.assertEqual(material.read_text(encoding="utf-8"), text)

    def test_writing_roundtrips_material_role_template_and_no_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = root / "results with spaces.markdown"
            material.write_text("# Results\nExternal result, not independently verified.\n", encoding="utf-8")
            config = self.prepare("--kind", "writing", "--goal", "Write an honest paper draft",
                                  "--material", str(material), "--template", "experiment",
                                  "--output-root", str(root / "runs"), "--prepare-only")
            defaults = research_defaults(["research-session", "--config", str(config)])
            self.assertEqual(defaults["task_kind"], "writing")
            self.assertEqual(defaults["material"], [str(material.resolve())])
            self.assertEqual(defaults["report_template"], "experiment")
            self.assertEqual(defaults["report_outline_strategy"], "adaptive")
            self.assertTrue(defaults["report_document_review"])
            self.assertEqual(defaults["outputs"], ["report"])
            self.assertNotIn("command_argv", defaults)
            self.assertNotIn("total_tokens", defaults)

    def test_writing_invalid_scope_does_not_save_a_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = root / "notes.md"
            material.write_text("Notes", encoding="utf-8")
            invalid = root / 'invalid.json'
            invalid.write_text('{"unfinished":', encoding='utf-8')
            for extra in (["--sources", "search"], ["--document", str(material)], ["--fulltext"],
                          ["--command", "python", "run.py"], ["--material", str(invalid)]):
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    self.prepare("--kind", "writing", "--goal", "Write from material", "--material", str(material),
                                 "--output-root", str(root / "runs"), "--prepare-only", *extra)
            self.assertFalse((root / "runs").exists())

    def test_writing_checks_template_before_saving_or_calling_a_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = root / "notes.md"
            material.write_text("Notes", encoding="utf-8")
            empty_template = root / "empty.md"
            empty_template.write_text("", encoding="utf-8")
            for template in ("experimnt", str(root / "missing.md"), str(empty_template)):
                with self.subTest(template=template), self.assertRaisesRegex(ValueError, "Invalid writing template"):
                    self.prepare("--kind", "writing", "--goal", "Draft", "--material", str(material),
                                 "--template", template, "--output-root", str(root / "runs"), "--prepare-only")
            self.assertFalse((root / "runs").exists())

    def prepare(self, *arguments):
        from simple_ar.cli.main import _explicit_session_option_destinations
        parser = build_parser()
        argv = ["start", *arguments]
        args = parser.parse_args(argv)
        args._explicit_start_destinations = _explicit_session_option_destinations(parser, argv, set(), command="start")
        with patch("sys.stdin.isatty", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            return prepare_start(args)

    def test_local_survey_roundtrips_canonical_config_without_api_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = root / "材料 one's source.md"
            paper.write_text("# A source\n", encoding="utf-8")
            config = self.prepare("--kind", "survey", "--goal", '比较 "A" 和 B\n说明局限',
                                  "--document", str(paper), "--sources", "materials",
                                  "--output-root", str(root / "runs"), "--prepare-only")
            defaults = research_defaults(["research-session", "--config", str(config)])
            self.assertEqual(defaults["task_kind"], "survey")
            self.assertEqual(defaults["local_document"], [str(paper.resolve())])
            self.assertTrue(defaults["research_materials_only"])
            self.assertEqual(defaults["output_root"], str((config.parent / "sessions").resolve()))
            self.assertEqual(defaults["model"], "env")
            self.assertNotIn("total_tokens", defaults)
            self.assertNotIn("execution_details", defaults)

    def test_exact_code_start_uses_native_contract_without_structured_setup(self):
        from simple_ar.cli.start import migrate_new_start
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project with spaces"
            project.mkdir()
            data = project / "data.csv"
            data.write_text("value\n7\n", encoding="utf-8")
            interpreter = root / "python"
            interpreter.touch()
            args = build_parser().parse_args(["start", "--kind", "bug_fix", "--goal", "Fix the rounding error",
                "--project", str(project), "--allow", "src/**", "--validate", "python tests/check.py",
                "--project-python", str(interpreter), "--data-path", str(data), "--timeout-sec", "45", "--prepare-only"])
            with patch("sys.stdin.isatty", return_value=False), patch("simple_ar.cli.start.print_line"):
                self.assertTrue(migrate_new_start(args))
            self.assertEqual(args._code_task["project_path"], "source1")
            self.assertEqual(args.source, [data.resolve(), project.resolve()])
            self.assertIn('"data_path": [', args.request)
            self.assertIn(json.dumps(str(data.resolve()), ensure_ascii=False), args.request)
            self.assertEqual(args._code_task["allowed_patterns"], ["src/**"])
            self.assertEqual(args._code_task["validation_command"], ["python", "tests/check.py"])
            self.assertEqual(args._code_task["timeout_sec"], 45)
            self.assertTrue(args.allow_execution)
            self.assertEqual(args.project_python, interpreter)
            self.assertIn("implement_code_task", args.request)
            self.assertEqual(list(project.iterdir()), [data])
            self.assertEqual(data.read_text(encoding="utf-8"), "value\n7\n")
            with patch("simple_ar.cli.main._run_native_start") as native, patch("sys.stdin.isatty", return_value=False):
                routed = build_parser().parse_args(["start", "--kind", "bug_fix", "--goal", "Fix",
                    "--project", str(project), "--allow", "src/**", "--validate", "python tests/check.py", "--prepare-only"])
                self.assertIsNone(prepare_start(routed))
            native.assert_called_once()
            self.assertFalse(list(root.rglob("research.toml")))
            self.assertFalse(list(root.rglob("code_task.toml")))

    def test_greenfield_exact_configuration_retains_its_existing_method(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = self.prepare("--kind", "code", "--goal", "Build a reusable CSV validator",
                "--allow", "generated_project/**", "--validate", "python generated_project/main.py",
                "--with-report", "--output-root", str(root / "new-code"), "--prepare-only")
            defaults = research_defaults(["research-session", "--config", str(created)])
            options = load_code_task_init_options(config_path=defaults["code_task_config"])
            self.assertEqual(defaults["outputs"], ["code_task", "report"])
            self.assertEqual(options.kind, "greenfield")
            self.assertIsNone(options.code_root)
            self.assertEqual(options.workspace_mode, "empty")
            self.assertEqual(defaults['report_outline_strategy'], 'adaptive')
            self.assertTrue(defaults['report_document_review'])

    def test_fulltext_is_explicit_and_maps_to_existing_retrieval_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            for flags, expected in (([], False), (["--fulltext"], True)):
                config = self.prepare("--kind", "survey", "--goal", "Compare methods", "--sources", "search",
                                      "--output-root", directory, "--prepare-only", *flags)
                values = research_defaults(["research-session", "--config", str(config)])
                self.assertEqual(values["research_use_fulltext"], expected)
                self.assertEqual(values["research_allow_pdf_download"], expected)
                self.assertEqual(values["research_keep_raw_pdf"], expected)
                if expected:
                    self.assertEqual(values["research_max_fulltext_documents"], 6)
                    self.assertEqual(values["research_max_pdf_mb"], 20)
                else:
                    self.assertNotIn("research_max_fulltext_documents", values)
                    self.assertNotIn("research_max_pdf_mb", values)
            with self.assertRaisesRegex(ValueError, "requires explicit --sources search"):
                self.prepare("--kind", "bug_fix", "--goal", "Fix", "--fulltext", "--prepare-only")

    def test_explicit_report_source_bound_roundtrips_without_affecting_other_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.prepare("--kind", "survey", "--goal", "Compare evidence across fields",
                                  "--sources", "search", "--max-cited-sources", "3",
                                  "--output-root", directory, "--prepare-only")
            values = research_defaults(["research-session", "--config", str(config)])
            self.assertEqual(values["report_max_cited_sources"], 3)
            with self.assertRaisesRegex(ValueError, "positive"):
                self.prepare("--kind", "survey", "--goal", "Compare evidence", "--sources", "search",
                             "--max-cited-sources", "0", "--output-root", directory, "--prepare-only")

    def test_bad_inputs_create_no_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runs"
            for extra in (
                ["--kind", "survey", "--sources", "materials"],
                ["--kind", "survey", "--sources", "search", "--allow", "src/**"],
                ["--kind", "bug_fix", "--project", directory, "--validate", "python tests.py", "--allow", "../*"],
                ["--kind", "survey", "--goal", " "],
            ):
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    self.prepare("--goal", "task", "--prepare-only", "--output-root", str(root), *extra)
                self.assertFalse(root.exists())

    def test_noninteractive_requires_execution_confirmation_but_keeps_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "--yes"):
                self.prepare("--kind", "survey", "--goal", "task", "--sources", "search", "--output-root", directory)
            configs = list(Path(directory).glob("*/research.toml"))
            self.assertEqual(len(configs), 1)
            self.assertEqual(tomllib.loads(configs[0].read_text())["task"]["goal"], "task")

    def test_technical_start_executes_shared_arguments_without_reading_export(self):
        from simple_ar.cli.main import main
        from simple_ar.cli.research_config import validate_session_arguments
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = root / "paper.md"
            paper.write_text("Published claim", encoding="utf-8")
            data = root / "values.csv"
            data.write_text("score\n1\n2\n", encoding="utf-8")
            cases = [
                ["--kind", "survey", "--sources", "search", "--fulltext", "--max-cited-sources", "3"],
                ["--kind", "data_analysis", "--data-file", str(data), "--value-column", "score",
                 "--observation-unit", "run"],
                ["--kind", "reproduction", "--document", str(paper), "--hypothesis", "Claim",
                 "--dataset", "Fixed data", "--expected-outcome", "Compare score", "--metric", "score",
                 "--project", directory, "--allow", "adapter.py", "--validate", "python check.py",
                 "--environment", "venv", "--timeout-sec", "12", "--command", "python", "adapter.py"],
            ]
            for number, flags in enumerate(cases):
                output = root / str(number)
                with self.subTest(flags=flags), patch("sys.stdin.isatty", return_value=False), \
                        patch("simple_ar.cli.main._print_research_session") as run, \
                        patch("simple_ar.cli.research_config.research_defaults", return_value={}) as load, \
                        contextlib.redirect_stdout(io.StringIO()):
                    main(["start", "--goal", "Preserve the confirmed contract", "--yes",
                          "--output-root", str(output), *flags])
                run.assert_called_once()
                self.assertEqual(load.call_count, 1)
                self.assertEqual(load.call_args.args[0][0], "start")
                prepared = run.call_args.args[0]
                self.assertEqual(prepared.command, "research-session")
                validate_session_arguments(prepared)
                exported = research_defaults(["research-session", "--config", str(prepared.config)])
                for destination, value in exported.items():
                    actual = getattr(prepared, destination)
                    self.assertEqual(str(actual) if isinstance(actual, Path) else actual, value, destination)
                self.assertEqual(prepared._explicit_resume_destinations,
                                 (set(exported) - {"execution_details"}) | {"config"})

    def test_technical_setup_confirmation_uses_edits_and_preserves_save_only(self):
        from simple_ar.cli.main import main
        with tempfile.TemporaryDirectory() as directory:
            for mode in ("decline", "prepare", "accept", "edit", "invalid"):
                output = Path(directory) / mode
                def confirm(prompt):
                    self.assertIn("Start now", prompt)
                    config, = output.glob("*/research.toml")
                    if mode == "edit":
                        config.write_text('[task]\ngoal="Edited goal"\nkind="survey"\n'
                            'outputs=["summary"]\noutput_root="edited sessions"\n'
                            '[model]\nname=""\n[research]\ninteraction="autonomous"\n', encoding="utf-8")
                    elif mode == "invalid":
                        config.write_text('[task]\ngoal="Edited goal"\noutput_root=3\n', encoding="utf-8")
                    return "n" if mode == "decline" else "y"
                with self.subTest(mode=mode), patch("sys.stdin.isatty", return_value=mode != "prepare"), \
                        patch("builtins.input", side_effect=confirm) as approval, \
                        patch("simple_ar.cli.main._print_research_session") as run, \
                        patch("simple_ar.cli.research_config.research_defaults", wraps=research_defaults) as load, \
                        contextlib.redirect_stdout(io.StringIO()):
                    argv = ["start", "--kind", "survey", "--sources", "search", "--fulltext", "--goal", "Keep inputs",
                            "--output-root", str(output), *(["--prepare-only"] if mode == "prepare" else [])]
                    if mode == "invalid":
                        with self.assertRaisesRegex(SystemExit, "Invalid type for task.output_root"):
                            main(argv)
                    else:
                        main(argv)
                self.assertEqual(len(list(output.glob("*/research.toml"))), 1)
                self.assertEqual(approval.call_count, 0 if mode == "prepare" else 1)
                self.assertEqual(load.call_count, 2 if mode in {"edit", "invalid"} else 1)
                if mode in {"decline", "prepare", "invalid"}:
                    run.assert_not_called()
                    continue
                run.assert_called_once()
                args = run.call_args.args[0]
                if mode == "edit":
                    self.assertEqual(args.topic, "Edited goal")
                    self.assertEqual(args.model, "")
                    self.assertEqual(args.outputs, ["summary"])
                    self.assertEqual(args.output_root, str((args.config.parent / "edited sessions").resolve()))
                    self.assertEqual(args.interaction, "autonomous")
                    self.assertNotIn("report_draft_scope", args._explicit_resume_destinations)
                    self.assertFalse(getattr(args, "research_allow_pdf_download", False))
                else:
                    self.assertEqual(args.topic, "Keep inputs")
                    self.assertTrue(args.research_allow_pdf_download)

    def test_interactive_native_decline_creates_no_task_or_legacy_configuration(self):
        from simple_ar.cli.main import main
        with tempfile.TemporaryDirectory() as directory:
            from unittest.mock import MagicMock
            app = MagicMock()
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=[
                "survey", "Review this direction", "", "none", "n",
            ]) as prompts, patch("simple_ar.cli.start.select_start_model"), \
                    patch("simple_ar.app.tool_application.ToolApplication.create", return_value=app) as create, \
                    contextlib.redirect_stdout(io.StringIO()):
                main(["start", "--guided", "--output-root", directory])
            create.assert_not_called()
            app.run.assert_not_called()
            self.assertFalse(list(Path(directory).glob("*/research.toml")))
            self.assertEqual(prompts.call_count, 5)

    def test_explicit_source_scope_never_prompts_for_optional_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            args = build_parser().parse_args(["start", "--kind", "survey", "--goal", "task",
                "--sources", "search", "--yes", "--output-root", directory])
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input") as prompt, \
                    contextlib.redirect_stdout(io.StringIO()):
                config = prepare_start(args)
            self.assertTrue(config.is_file())
            prompt.assert_not_called()

    def test_interactive_preferences_require_explicit_access_and_retain_named_model(self):
        from simple_ar.cli.main import main
        with tempfile.TemporaryDirectory() as directory:
            material = Path(directory) / "notes.md"
            material.write_text("# Supplied observations\n", encoding="utf-8")
            for choice, network, process in (("none", False, False), ("network", True, False),
                                             ("process", False, True), ("both", True, True)):
                with self.subTest(access=choice), patch("sys.stdin.isatty", return_value=True), \
                        patch("builtins.input", side_effect=["4", choice]), \
                        patch("simple_ar.cli.main._run_native_start") as native, \
                        contextlib.redirect_stdout(io.StringIO()):
                    main(["start", "--guided", "--material", str(material), "--model", "profile:chosen",
                          "--goal", "Summarize supplied notes", "--output-root", directory])
                adapted = native.call_args.args[0]
                self.assertEqual(adapted.model, "profile:chosen")
                self.assertFalse(getattr(adapted, "_model_confirmed", False))
                self.assertEqual(adapted.allow_network, network)
                self.assertEqual(adapted.allow_execution, process)
                self.assertEqual(adapted.approve_execution, "")

    def test_invalid_interactive_selection_creates_no_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "output"
            args = build_parser().parse_args(["start", "--guided", "--output-root", str(root)])
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="99"), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "Choose"):
                prepare_start(args)
            self.assertFalse(root.exists())

    def test_native_resume_preserves_existing_authorization_and_state(self):
        from simple_ar.cli.main import main
        from simple_ar.core.budget import BudgetLedger
        from unittest.mock import MagicMock
        app = MagicMock()
        app.run.return_value = {'status': 'completed', 'status_reason': 'verified'}
        app.session.store.root = Path('saved-task').resolve()
        app.session.store.exists.return_value = False
        app.workspace.workspace = app.session.store.root / 'workspace'
        app.state = {'config': {'model': 'profile:saved', 'execution_contract': None},
            'pending': None, 'requests': ['Saved goal'], 'turns': []}
        app.measurement_completion_actions.return_value = []
        app.ledger = BudgetLedger({'llm_requests': 2, 'total_tokens': 100, 'process_wall_seconds': 20})
        app.ledger.reserve('old-call', {'llm_requests': 2, 'total_tokens': 100})
        app.ledger.settle('old-call', {'llm_requests': 2, 'total_tokens': 100})
        with patch('simple_ar.app.tool_application.ToolApplication.load', return_value=app), \
                patch('sys.stdin.isatty', return_value=False), contextlib.redirect_stdout(io.StringIO()):
            continuation = ['start', '--resume-task', 'saved-task', '--source', 'extra notes', '--allow-network']
            main(continuation)
            app.run.assert_not_called()
            main([*continuation, '--prepare-only'])
            app.run.assert_not_called()
            main([*continuation, '--yes'])
            self.assertIsNone(app.run.call_args.kwargs['resource_allowances'])
            self.assertEqual(app.run.call_args.kwargs['read_only_sources'], (Path('extra notes'),))
            self.assertTrue(app.run.call_args.kwargs['allow_network'])
            self.assertFalse(app.run.call_args.kwargs['allow_execution'])
            app.run.reset_mock()
            with self.assertRaisesRegex(SystemExit, 'separately'):
                main([*continuation, '--yes', '--approve-execution', 'old-command'])
            app.run.assert_not_called()
            resources = ['start', '--resume-task', 'saved-task', 'Continue the saved goal',
                '--authorize-remaining', 'llm_requests=3', '--authorize-remaining', 'total_tokens=500',
                '--authorize-remaining', 'process_wall_seconds=2.5',
                '--authorization-id', 'continuation-1', '--authorization-reason', 'Finish authorized work']
            before = [entry.to_dict() for entry in app.ledger.entries]
            with patch('simple_ar.core.console.make_console') as console:
                for extra in ([], ['--prepare-only', '--yes']):
                    main([*resources, *extra])
                    app.run.assert_not_called()
                summary = console.return_value.print.call_args_list[0].args[0].renderable.plain
                self.assertIn("'total_tokens': 100", summary)
                self.assertIn('Original cumulative usage is not cleared', summary)
                self.assertIn('New remaining is not a total historical limit', summary)
                main([*resources, '--yes', '--max-tokens', '9000', '--max-turns', '99'])
            forwarded = app.run.call_args.kwargs
            self.assertEqual(forwarded['resource_allowances'],
                {'llm_requests': 3, 'total_tokens': 500, 'process_wall_seconds': 2.5})
            self.assertEqual(forwarded['authorization_id'], 'continuation-1')
            self.assertEqual(forwarded['authorization_reason'], 'Finish authorized work')
            self.assertEqual(forwarded['feedback'], 'Continue the saved goal')
            self.assertEqual(forwarded['approved_execution'], '')
            self.assertFalse(forwarded['allow_execution'])
            self.assertNotIn('model', forwarded)
            self.assertNotIn('max_tokens', forwarded)
            self.assertNotIn('max_turns', forwarded)
            self.assertEqual([entry.to_dict() for entry in app.ledger.entries], before)
            app.run.reset_mock()
            with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='n'):
                main(resources)
                app.run.assert_not_called()
            with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='y'):
                main(resources)
                app.run.assert_called_once()
            app.run.reset_mock()
            for argv, message in (
                ([*resources, '--yes', '--approve-execution', 'old-command'], 'separately'),
                (['start', '--authorize-remaining', 'total_tokens=500'], '--resume-task'),
                (['start', '--resume-task', 'saved-task', '--authorization-id', 'orphan'], 'require --authorize-remaining'),
                (['start', '--resume-task', 'saved-task', '--authorization-reason', 'orphan'], 'require --authorize-remaining'),
                (['start', '--resume-task', 'saved-task', '--authorize-remaining', 'total_tokens=500'], 'requires --authorization-id'),
                (['start', '--resume-task', 'saved-task', '--authorize-remaining', 'total_tokens=500', '--authorization-id', 'missing-reason'], 'requires --authorization-id'),
                (['start', '--resume-task', 'saved-task', '--authorize-remaining', 'total_tokens=500', '--authorization-reason', 'missing-id'], 'requires --authorization-id'),
                ([*resources, '--authorization-reason', ' '], 'requires --authorization-id'),
                ([*resources, '--authorize-remaining', 'total_tokens=600'], 'Duplicate'),
                ([*resources, '--authorize-remaining', 'bad-format'], 'DIMENSION=AMOUNT'),
            ):
                with self.subTest(argv=argv), self.assertRaisesRegex(SystemExit, message):
                    main(argv)
                app.run.assert_not_called()
            main(['start', '--resume-task', 'saved-task', '--max-tokens', '9000', '--max-turns', '99',
                '--approve-execution', 'old-command'])
            self.assertIsNone(app.run.call_args.kwargs['resource_allowances'])
            self.assertEqual(app.run.call_args.kwargs['approved_execution'], 'old-command')
            self.assertFalse(app.run.call_args.kwargs['retry_model'])
            app.run.reset_mock()
            main(['start', '--resume-task', 'saved-task', '--retry-model', '--prepare-only'])
            app.run.assert_not_called()
            main(['start', '--resume-task', 'saved-task', '--retry-model'])
            self.assertTrue(app.run.call_args.kwargs['retry_model'])
            self.assertEqual(app.run.call_args.kwargs['feedback'], '')
            app.run.reset_mock()
            for argv in (['start', '--retry-model'],
                    ['start', '--kind', 'code', '--retry-model'],
                    ['start', '--resume-task', 'saved-task', '--retry-model', 'new task'],
                    ['start', '--resume-task', 'saved-task', '--retry-model', '--allow-network'],
                    ['start', '--resume-task', 'saved-task', '--retry-model', '--approve-execution', 'token'],
                    [*resources, '--retry-model', '--yes']):
                with self.subTest(retry_argv=argv), self.assertRaises(SystemExit):
                    main(argv)
                app.run.assert_not_called()

    def test_native_readout_preserves_saved_bytes_accounting_and_acceptance_boundary(self):
        from simple_ar.cli.main import _render_native_result
        from simple_ar.core.budget import BudgetLedger
        from simple_ar.core.capabilities import ArtifactStore
        from simple_ar.tools.workspace import resolve_mounted_path
        from types import SimpleNamespace
        from rich.console import Console
        from unittest.mock import MagicMock
        app = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            app.session.store = ArtifactStore(root)
            workspace = root / 'workspace'
            workspace.mkdir()
            app.workspace = SimpleNamespace(workspace=workspace,
                resolve=lambda locator: resolve_mounted_path({'workspace': workspace}, locator))
            app.state = {'requests': ['Initial draft', 'Feedback'],
                         'pending': {'kind': 'model', 'attempt': 'native-turn-0002'}}
            app.ledger = BudgetLedger({'llm_requests': 3, 'total_tokens': 500})
            app.ledger.reserve('draft', {'llm_requests': 1, 'total_tokens': 100})
            app.ledger.settle('draft', {'llm_requests': 1, 'total_tokens': 100})
            app.ledger.reserve('unreconciled', {'llm_requests': 1, 'total_tokens': 300})
            app.ledger.mark_unknown('unreconciled', reason='Interrupted', known_actual={'llm_requests': 1})
            app.ledger.reserve('in-flight', {'llm_requests': 1})
            app.session.store.write_text('workspace/draft.md', 'Initial draft')
            app.session.store.write_text('workspace/unlisted.md', 'Not a delivery inventory')
            app.session.store.write_json('delivery.json', {'summary': 'Initial draft',
                'artifacts': ['workspace/draft.md', 'workspace/missing.pdf', 'workspace/.env',
                              '../outside.pdf', 'workspace/unlisted.md'], 'unresolved': []})
            before = {path: path.read_bytes() for path in root.rglob('*') if path.is_file()}
            ledger_before = app.ledger.to_dict()
            state_before = json.dumps(app.state)
            for saved_status, reason in (
                ('paused', 'BudgetExceededError: requested 400, remaining 100'),
                ('paused', 'RuntimeError: Operation failed; inspect saved diagnostics: LLMError: '
                 "LLM budget prevented provider attempt 1: Budget exceeded for 'total_tokens'"),
                ('paused', 'LLMError: https://private-provider.invalid api_key=secret'),
                ('completed', 'Delivery manifest saved; scientific quality requires independent checking'),
                ('partial', 'Delivery manifest saved; scientific quality requires independent checking'),
            ):
                output = io.StringIO()
                with self.subTest(status=saved_status, reason=reason), contextlib.redirect_stdout(output), \
                        patch('simple_ar.core.console.make_console', return_value=Console(file=output, width=500)), \
                        patch.object(app, 'run', side_effect=AssertionError('readout must not run')):
                    _render_native_result(app, {'status': saved_status, 'status_reason': reason})
                text = output.getvalue()
                self.assertIn(f'Status: {saved_status}', text)
                self.assertIn('Saved delivery record', text)
                self.assertIn('Request/version binding not checked', text)
                self.assertIn('native-turn-0002', text)
                self.assertIn('draft.md', text)
                self.assertIn('missing or not a file', text)
                self.assertIn('outside allowed paths', text)
                self.assertIn('settled=100', text)
                self.assertIn('unreconciled=1', text)
                self.assertIn('unknown', text)
                self.assertIn('not establish acceptance', text)
                self.assertNotIn('unlisted.md', text)
                self.assertNotIn('.env', text)
                self.assertNotIn('private-provider', text)
                self.assertNotIn('api_key', text)
                if saved_status == 'paused':
                    self.assertIn('Current request has no confirmed delivery', text)
                if 'budget prevented provider attempt' in reason or reason.startswith('BudgetExceededError:'):
                    self.assertIn('Insufficient remaining budget before dispatch', text)
            self.assertEqual(app.ledger.to_dict(), ledger_before)
            self.assertEqual(json.dumps(app.state), state_before)
            self.assertEqual({path: path.read_bytes() for path in root.rglob('*') if path.is_file()}, before)
            for record in ({'artifacts': None}, None):
                app.session.store.write_json('delivery.json', record)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    _render_native_result(app, {'status': 'paused', 'status_reason': ''})
                self.assertIn('Saved delivery record unreadable', output.getvalue())
            (root / 'delivery.json').unlink()
            with contextlib.redirect_stdout(output := io.StringIO()):
                _render_native_result(app, {'status': 'paused', 'status_reason': ''})
            self.assertIn('No saved delivery record', output.getvalue())

    def test_native_and_explicit_protocol_entry_routes_remain_distinct(self):
        from simple_ar.cli.main import main
        with patch('simple_ar.cli.main._run_native_start') as native, \
                patch('simple_ar.cli.start.new_research_session_root') as legacy:
            # Bare/goal spellings share the ordinary-preferences test above.
            for argv in (['start', '--source', 'results with spaces', '--prepare-only'],
                         ['start', '--allow-network', '--model', 'profile:research']):
                with self.subTest(argv=argv):
                    main(argv)
            self.assertEqual(native.call_count, 2)
            legacy.assert_not_called()
            with self.assertRaisesRegex(SystemExit, 'Do not mix'):
                main(['start', 'Write a report', '--template', 'experiment'])
        with tempfile.TemporaryDirectory() as directory, patch("sys.stdin.isatty", return_value=False), \
                patch("simple_ar.cli.main._print_research_session") as run, contextlib.redirect_stdout(io.StringIO()):
            main(["start", "--kind", "survey", "--goal", "task", "--sources", "search",
                  "--output-root", directory, "--yes"])
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0].task_kind, "survey")
            self.assertEqual(run.call_args.args[0].outputs, ["report"])
        from simple_ar.cli.start import migrate_measurement_creation
        with tempfile.TemporaryDirectory() as directory, \
                patch('simple_ar.app.tool_application.ToolApplication.create') as create, \
                patch('simple_ar.cli.main._render_native_result'), patch('simple_ar.cli.start.print_line') as print_line:
            root = Path(directory).resolve()
            execution = {'command': [sys.executable, '-c', "print('score: 3')"], 'cwd': str(root),
                'timeout_sec': 5, 'result_schema': {'primary_metric': 'score'}}
            limits = {'llm_requests': 3, 'total_tokens': 24000,
                'process_invocations': 2, 'process_wall_seconds': 10}
            for model in (None, 'fixture'):
                args = build_parser().parse_args(['research-session', '--topic', 'Fixed measurement', '--task-kind', 'measurement',
                    *(['--model', model] if model else [])])
                args._explicit_resume_destinations = {'task_kind', 'model'}
                with self.assertRaisesRegex(ValueError, 'explicit interpreters'):
                    migrate_measurement_creation(args, execution={**execution, 'environment': {'mode': 'venv'}},
                        request='Do not replace the selected interpreter', outputs=('experiments',), root=root, limits=limits)
                with self.assertRaisesRegex(ValueError, 'baseline_ref requires baseline_policy=reuse'):
                    migrate_measurement_creation(args, execution={**execution, 'baseline_ref': 'historical.json'},
                        request='Reject an unrequested baseline', outputs=('experiments',), root=root, limits=limits)
                self.assertFalse(migrate_measurement_creation(args, execution={**execution, 'baseline_policy': 'reuse'},
                    request='Keep historical reuse with its owner', outputs=('experiments',), root=root, limits=limits))
                self.assertFalse(migrate_measurement_creation(args, execution=execution,
                    request='Keep explicit outputs', outputs=('report',), root=root, limits=limits))
                if model is None:
                    with self.assertRaisesRegex(ValueError, 'authored report requires a model'):
                        migrate_measurement_creation(args, execution=execution,
                            request='Do not fabricate a report', outputs=('experiments', 'report'), root=root, limits=limits)
                create.assert_not_called()
                if model:
                    for key, value in (('llm_requests', 0), ('total_tokens', 0), ('total_tokens', -1)):
                        with self.assertRaisesRegex(ValueError, 'finite positive'):
                            migrate_measurement_creation(args, execution=execution, request='Bounded creation',
                                outputs=('experiments',), root=root, limits={**limits, key: value})
                create.assert_not_called()

            args = build_parser().parse_args(['research-session', '--topic', 'Measure declared conditions', '--task-kind', 'measurement'])
            args.model = None
            args._explicit_resume_destinations = {'task_kind'}
            grouped = {**execution, 'baseline_policy': 'run', 'seeds': [4, 9], 'seed_flag': '--seed',
                'environment': {'mode': 'current', 'timeout_sec': 5,
                    'check_command': [sys.executable, '-c', 'pass']}}
            app = create.return_value
            app.state = {'turns': [{'origin': 'caller'}], 'pending': {'attempt': 'first'}}
            app.measurement_completion_actions.return_value = []
            def run_batch(*, actions, approve, progress):
                self.assertEqual(len(actions), 4)
                for index, action in enumerate(actions):
                    call = {'id': f'caller-0-{index}', 'function': {'name': action['tool_name'],
                        'arguments': json.dumps(action['arguments'])}}
                    changed = {**call, 'function': {**call['function'], 'arguments': json.dumps({
                        **action['arguments'], 'timeout_sec': 99})}}
                    self.assertFalse(approve(changed, 'changed-argv'))
                    app.state['turns'][0]['origin'] = 'model'
                    self.assertFalse(approve(call, 'model-must-not-inherit-caller-approval'))
                    app.state['turns'][0]['origin'] = 'caller'
                    app.scientific.approval_binding.return_value = ({'environment_preparation': {
                        'steps': [[sys.executable, '-c', 'pass']], 'profile': {'timeout_sec': 5}},
                        'limits': limits} if index == 0 else {'shared_environment': {'attempt_id': 'first'}})
                    self.assertTrue(approve(call, 'literal-initial-condition'))
                    self.assertFalse(approve(call, 'never-retry'))
                return {'status': 'completed'}
            app.run.side_effect = run_batch
            self.assertTrue(migrate_measurement_creation(args, execution=grouped, request='Prepare once for all roles',
                outputs=('experiments',), root=root, limits=limits))
            options = create.call_args.kwargs
            self.assertIsNone(options['code_task'])
            self.assertEqual(options['process_limits'], {'process_invocations': 5, 'process_wall_seconds': 25})
            self.assertEqual(len(options['execution_contract']['conditions']), 4)
            args.process_invocations, args.process_wall_seconds = 2, 10
            migrate_measurement_creation(args, execution=grouped, request='Keep explicit limits',
                outputs=('experiments',), root=root, limits=limits)
            self.assertEqual(create.call_args.kwargs['process_limits'],
                {'process_invocations': 2, 'process_wall_seconds': 10})
            create.reset_mock()
            # Reject an incompatible baseline cwd before creating the common task.
            other = root / 'other'
            other.mkdir()
            with self.assertRaisesRegex(ValueError, 'same declared cwd'):
                migrate_measurement_creation(args, execution={**grouped, 'baseline': {
                    'command': execution['command'], 'cwd': str(other)}}, request='Invalid shared cwd',
                    outputs=('experiments',), root=root, limits=limits)
            create.assert_not_called()
            with self.assertRaises(ValueError):
                migrate_measurement_creation(args, execution={**grouped, 'baseline': {
                    'command': execution['command'], 'environment': {'mode': 'venv'}}},
                    request='No per-condition environment replacement', outputs=('experiments',), root=root, limits=limits)
            create.assert_not_called()

            # Fixed research keeps one owner, but investigation/design must
            # precede separately approved execution of the declared comparison.
            from simple_ar.tools.sources import SourcePolicy
            paper, notes, data = (root / name for name in ('paper.md', 'notes.md', 'data.csv'))
            for path, content in ((paper, 'Published claim'), (notes, 'Unverified notes'),
                                  (data, 'score\n3\n')):
                path.write_text(content, encoding='utf-8')
            originals = {path: path.read_bytes() for path in (paper, notes, data)}
            sources = tuple({'locator': str(path), 'role': role} for path, role in
                            ((paper, 'paper'), (notes, 'material'), (data, 'dataset')))
            research_execution = {**execution, 'baseline_policy': 'skip',
                'label': 'Declared comparison',
                'protocol': {'hypothesis': 'Published claim', 'dataset': 'Supplied data',
                             'expected_outcome': 'Compare score to three'},
                'environment': {'mode': 'current', 'timeout_sec': 5,
                                'check_command': [sys.executable, '-c', 'pass']},
                'result_schema': {'primary_metric': 'score', 'required_metrics': ['score'],
                                  'output_files': {'scores': 'scores.json'},
                                  'metric_sources': {'score': {'output': 'scores', 'path': ['score']}}}}
            research = {'interaction': 'autonomous', 'research_max_documents': 7,
                'research_use_fulltext': True, 'research_allow_pdf_download': True,
                'research_keep_raw_pdf': True, 'research_max_fulltext_documents': 2,
                'research_max_pdf_mb': 9, 'research_max_pdf_pages': 31,
                'research_read_max_shortlist': 4, 'research_web_extract_backend': 'tavily_basic',
                'research_max_iterations': 3, 'research_selected_idea_id': 'idea-2',
                'feasibility_review_model': 'profile:review'}
            research_args = build_parser().parse_args(['research-session', '--topic', 'Investigate before measuring',
                '--task-kind', 'auto', '--interaction', 'autonomous', '--model', 'fixture',
                '--query', 'first exact query', '--query', 'second exact query',
                '--provider', 'openalex', '--provider', 'arxiv', '--max-results', '6',
                '--max-chunks', '19', '--idea-limit', '2', '--cache-dir', str(root / 'cache'),
                '--max-output-tokens', '1700'])
            research_args._explicit_resume_destinations = {'task_kind', 'interaction', 'model'}
            frozen_inputs = json.dumps([research_execution, research, limits, sources], sort_keys=True)
            app.run.side_effect = None
            app.run.return_value = {'status': 'paused', 'status_reason': 'Design and execution need approval'}
            for materials_only, explicit_budget, task_kind, interaction, reuse in (
                    (False, False, 'auto', 'autonomous', False),
                    (False, True, 'measurement', 'checkpoints', False),
                    (True, False, 'reproduction', 'assisted', False),
                    (True, False, 'measurement', 'checkpoints', True),
                    (False, True, 'auto', 'assisted', True)):
                with self.subTest(task_kind=task_kind, interaction=interaction, reuse=reuse), \
                        patch('subprocess.run') as process, \
                        patch('simple_ar.cli.start.new_research_session_root') as legacy:
                    create.reset_mock()
                    research_args.process_invocations = 2 if explicit_budget else None
                    research_args.process_wall_seconds = 10 if explicit_budget else None
                    research_args.task_kind, research_args.interaction = task_kind, interaction
                    declared_execution = {**research_execution, **({'baseline_policy': 'reuse',
                        'baseline_ref': str(data)} if reuse else {})}
                    # Also exercise explicit research controls without non-dataset material.
                    selected_sources = sources if not explicit_budget else sources[2:]
                    research_args._explicit_resume_destinations = {'task_kind', 'interaction', 'model'} | (
                        {'queries', 'providers', 'process_invocations', 'process_wall_seconds'}
                        if explicit_budget else set())
                    with self.assertRaisesRegex(SystemExit, "status 'paused'"):
                        migrate_measurement_creation(research_args, execution=declared_execution,
                            request='Investigate before measuring; commands require separate approval',
                            outputs=('experiments', 'summary'), root=root, limits=limits, sources=selected_sources,
                            research_config={**research, 'interaction': interaction,
                                             'research_materials_only': materials_only})
                    create.assert_called_once()
                    self.assertEqual(create.call_args.args[0], root)
                    options = create.call_args.kwargs
                    self.assertEqual(options['sources'], {f'source{i}': Path(row['locator'])
                        for i, row in enumerate(selected_sources)})
                    expected_policy = SourcePolicy(queries=('first exact query', 'second exact query'),
                        providers=('openalex', 'arxiv'), materials=() if explicit_budget else ('source0', 'source1'),
                        max_results=6, max_documents=7, max_chunks=19, idea_limit=2,
                        cache_dir=str(root / 'cache'), require_fulltext=True, allow_pdf_download=True,
                        keep_raw_pdf=True, max_fulltext_documents=2, max_pdf_mb=9, max_pdf_pages=31,
                        read_max_shortlist=4, web_extract_backend='tavily_basic',
                        allow_network=not materials_only, feasibility_connection='profile:review',
                        require_design=True, require_summary=True, max_iterations=3, interaction=interaction,
                        selected_idea_id='idea-2', execution_boundary={**declared_execution,
                            **({'baseline_ref': 'source0' if explicit_budget else 'source2'} if reuse else {})},
                        execution_schema=research_execution['result_schema']).model_dump(mode='json')
                    self.assertEqual(options['research_policy'], expected_policy)
                    self.assertEqual(options['allow_network'], not materials_only)
                    self.assertTrue(options['allow_execution'])  # Proposals still require separate approval.
                    self.assertIsNone(options['code_task'])
                    if reuse:
                        baseline, candidate = options['execution_contract']['conditions']
                        self.assertEqual(baseline['reuse_ref'], 'source0' if explicit_budget else 'source2')
                        self.assertEqual(candidate['role'], 'candidate')
                    self.assertEqual((options['max_turns'], options['max_tokens']), (3, 24000))
                    self.assertEqual(options['max_output_tokens'], 1700)
                    self.assertEqual(options['source_operations'], 4 + 7 * 3)
                    # Two exact queries x two providers reserve search work only by default.
                    self.assertEqual(options['process_limits'], {
                        'process_invocations': 2 if explicit_budget or materials_only else 2 + 4 * 4,
                        'process_wall_seconds': 10 if explicit_budget or materials_only else 10 + 4 * 120})
                    app.run.assert_called_once_with(progress=print_line)
                    app.measurement_completion_actions.assert_not_called()
                    self.assertEqual(app.scientific.mock_calls, [])
                    process.assert_not_called()
                    legacy.assert_not_called()
                    self.assertEqual({path: path.read_bytes() for path in originals}, originals)
            create.reset_mock()
            # Open comparison design and historical/no-model research retain fallback.
            for model, interaction, declared_execution in (
                    ('fixture', 'autonomous', execution),
                    (None, 'autonomous', research_execution),
                    ('fixture', 'checkpoints', execution),
                    ('fixture', 'assisted', {**research_execution, 'baseline_policy': 'reuse'})):
                research_args.model, research_args.interaction = model, interaction
                self.assertFalse(migrate_measurement_creation(research_args, execution=declared_execution,
                    request='Keep the original owner', outputs=('experiments',), root=root,
                    limits=limits, sources=sources, research_config=research))
            create.assert_not_called()
            self.assertEqual(json.dumps([research_execution, research, limits, sources], sort_keys=True), frozen_inputs)

    def test_fixed_measurement_research_is_selected_by_obligations_and_keeps_fallbacks(self):
        from copy import deepcopy
        from simple_ar.cli.start import migrate_measurement_creation
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            args = build_parser().parse_args(['research-session', '--task-kind', 'measurement',
                '--topic', 'Honor declared research obligations', '--model', 'fixture', '--interaction', 'assisted'])
            execution = {'command': [sys.executable, 'measure.py'], 'cwd': str(root),
                'timeout_sec': 5, 'result_schema': {'primary_metric': 'score'}, 'baseline_policy': 'skip'}
            limits = {'llm_requests': 3, 'total_tokens': 24000,
                'process_invocations': 2, 'process_wall_seconds': 10}
            for outputs, selected in ((('experiments', 'summary'), None), (('experiments',), 'idea-2')):
                args._explicit_resume_destinations = {'task_kind', 'interaction', 'model'} | (
                    {'selected_idea_id'} if selected else set())
                research = {'interaction': 'assisted', 'research_materials_only': True,
                    **({'research_selected_idea_id': selected} if selected else {})}
                original = deepcopy((vars(args), execution, limits, research))
                with self.subTest(outputs=outputs, selected=selected), \
                        patch('simple_ar.app.tool_application.ToolApplication.create') as create, \
                        patch('simple_ar.cli.main._render_native_result'), \
                        patch('simple_ar.cli.start.print_line') as progress, patch('subprocess.run') as process:
                    app = create.return_value
                    app.run.return_value = {'status': 'completed'}
                    self.assertTrue(migrate_measurement_creation(args, execution=execution,
                        request='Honor the declared research obligation', outputs=outputs, root=root,
                        limits=limits, research_config=research))
                    policy = create.call_args.kwargs['research_policy']
                    self.assertTrue(policy['require_design'])
                    self.assertEqual(policy['require_summary'], 'summary' in outputs)
                    self.assertEqual(policy['selected_idea_id'], selected)
                    self.assertEqual(policy['interaction'], 'assisted')
                    app.run.assert_called_once_with(progress=progress)
                    app.request_execution_confirmation.assert_not_called()
                    app.measurement_completion_actions.assert_not_called()
                    process.assert_not_called()
                self.assertEqual((vars(args), execution, limits, research), original)
            with patch('simple_ar.app.tool_application.ToolApplication.create') as create:
                # Unknown explicit controls cannot be silently dropped from research.
                args._explicit_resume_destinations.add('report_figures')
                self.assertFalse(migrate_measurement_creation(args, execution=execution,
                    request='Retain unmapped controls', outputs=('experiments',), root=root,
                    limits=limits, research_config=research))
                create.assert_not_called()

    def test_measurement_reuse_maps_read_only_sources_and_only_authorizes_initial_candidates(self):
        from copy import deepcopy
        from simple_ar.cli.start import migrate_measurement_creation
        from simple_ar.tools.scientific import freeze_measurement_set

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config_dir = root / 'config'
            config_dir.mkdir()
            (config_dir / 'archive').mkdir()
            config = config_dir / 'research.toml'
            config.write_text('[task]\ngoal="Reuse evidence"\n', encoding='utf-8')
            baselines = [config_dir / f'seed-{seed}.json' for seed in (4, 9)]
            notes = root / 'notes.md'
            notes.write_text('Supplied read-only material', encoding='utf-8')
            # Opaque inputs at the CLI boundary; the mocked factory owns canonical validation.
            for path in baselines:
                path.write_text('{"retained": true}\n', encoding='utf-8')
            originals = {path: path.read_bytes() for path in (config, notes, *baselines)}
            limits = {'llm_requests': 3, 'total_tokens': 24000,
                      'process_invocations': 2, 'process_wall_seconds': 10}
            for with_config, paired in ((True, False), (False, False), (True, True), (False, True)):
                with self.subTest(with_config=with_config, paired=paired):
                    args = build_parser().parse_args(['research-session', '--topic', 'Reuse evidence',
                        '--task-kind', 'measurement', *(['--config', str(config)] if with_config else [])])
                    args._explicit_resume_destinations = {'task_kind'}
                    prefix = '' if with_config else 'config/'
                    references = [prefix + 'archive/../seed-4.json', str(baselines[1])]
                    execution = {'command': [sys.executable, 'measure.py'], 'cwd': str(root),
                        'timeout_sec': 5, 'result_schema': {'primary_metric': 'score'},
                        'baseline_policy': 'reuse', 'baseline_ref': references if paired else references[0]}
                    if paired:
                        execution.update(seeds=[4, 9], seed_flag='--seed')
                    # The first baseline is already mounted through a different lexical path.
                    sources = ({'locator': str(notes), 'role': 'material'},
                               {'locator': str(config_dir / 'archive' / '..' / baselines[0].name),
                                'role': 'dataset'})
                    original = deepcopy((vars(args), execution, sources, limits))
                    with patch('pathlib.Path.cwd', return_value=root), \
                            patch('simple_ar.app.tool_application.ToolApplication.create') as create, \
                            patch('simple_ar.tools.scientific.freeze_measurement_set',
                                  wraps=freeze_measurement_set) as freeze, \
                            patch('simple_ar.cli.main._render_native_result'), \
                            patch('simple_ar.cli.start.print_line'), \
                            patch('subprocess.run') as process:
                        app = create.return_value
                        app.state = {'turns': [{'origin': 'caller'}], 'pending': None}
                        app.run.return_value = {'status': 'completed'}
                        app.measurement_completion_actions.return_value = []
                        self.assertTrue(migrate_measurement_creation(args, execution=execution,
                            request='Reuse evidence', outputs=('experiments',), root=root,
                            limits=limits, sources=sources))
                        self.assertEqual(freeze.call_args.args[0]['baseline_ref'],
                                         ['source1', 'source2'] if paired else 'source1')
                        options = create.call_args.kwargs
                        self.assertEqual({name: path.resolve() for name, path in options['sources'].items()},
                            {'source0': notes, 'source1': baselines[0],
                             **({'source2': baselines[1]} if paired else {})})
                        self.assertIsNone(options['code_task'])
                        conditions = options['execution_contract']['conditions']
                        self.assertEqual([(row['seed'], row['reuse_ref']) for row in conditions
                                          if row['role'] == 'baseline'],
                                         [(4, 'source1'), (9, 'source2')] if paired else [(None, 'source1')])
                        app.run.assert_called_once()
                        actions = app.run.call_args.kwargs['actions']
                        self.assertEqual([row['arguments']['condition_id'] for row in actions],
                                         ['candidate:0', 'candidate:1'] if paired else ['candidate:0'])
                        if paired:
                            self.assertEqual([row['arguments']['command'] for row in actions],
                                [[sys.executable, 'measure.py', '--seed', str(seed)] for seed in (4, 9)])
                        approve = app.run.call_args.kwargs['approve']
                        for index, action in enumerate(actions):
                            call = {'id': f'caller-0-{index}', 'function': {
                                'name': action['tool_name'], 'arguments': json.dumps(action['arguments'])}}
                            altered = deepcopy(call)
                            altered['function']['arguments'] = json.dumps({**action['arguments'], 'timeout_sec': 99})
                            self.assertFalse(approve(altered, 'altered'))
                            app.state['turns'][0]['origin'] = 'model'
                            self.assertFalse(approve(call, 'model'))
                            app.state['turns'][0]['origin'] = 'caller'
                            self.assertTrue(approve(call, 'initial'))
                            self.assertFalse(approve(call, 'retry'))
                        process.assert_not_called()
                    self.assertEqual((vars(args), execution, sources, limits), original)
                    self.assertEqual({path: path.read_bytes() for path in originals}, originals)

    def test_research_code_creation_retains_domain_contracts_in_one_task(self):
        from copy import deepcopy
        from simple_ar.app.research_execution import code_task_execution_options
        from simple_ar.cli.start import migrate_exact_code_creation
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / 'project'
            project.mkdir()
            (project / 'check.py').write_text('pass\n', encoding='utf-8')
            paper = root / 'paper.md'
            paper.write_text('Source method and conditions', encoding='utf-8')
            task = code_task_execution_options(load_code_task_init_options(kind='existing_project',
                code_root=str(project), workspace_mode='copy', require_task_file=False),
                load_code_task_execute_options())
            task['validation_command'] = [sys.executable, 'check.py']
            task['validation_timeout_sec'] = 5
            execution = {'command': [sys.executable, 'measure.py'], 'cwd': str(project),
                'timeout_sec': 5, 'result_schema': {'required_metrics': ['score']},
                'baseline_policy': 'skip', 'code_task': task}
            args = build_parser().parse_args(['research-session', '--task-kind', 'auto', '--topic', 'Investigate and implement',
                '--model', 'fixture', '--interaction', 'autonomous', '--query', 'method conditions', '--provider', 'arxiv'])
            args._explicit_resume_destinations = {'task_kind', 'model', 'interaction', 'queries', 'providers'}
            limits = {'llm_requests': 4, 'total_tokens': 50000, 'process_invocations': 3, 'process_wall_seconds': 15}
            research = {'interaction': 'autonomous', 'research_materials_only': True,
                'research_max_documents': 2, 'research_max_iterations': 1}
            sources = ({'locator': str(paper), 'role': 'paper'},)
            before = deepcopy((vars(args), execution, research, limits))
            with patch('simple_ar.cli.main._run_native_start') as run, patch('simple_ar.cli.start.print_line'):
                self.assertTrue(migrate_exact_code_creation(args, execution=execution,
                    request=args.topic, outputs=('experiments',), sources=sources, limits=limits,
                    root=root / 'task', research_config=research))
            native = run.call_args.args[0]
            self.assertEqual(native.source, [project, paper])
            self.assertEqual(native._code_task['project_path'], 'source0')
            self.assertTrue(native._research_policy['require_design'])
            self.assertFalse(native.allow_network)
            self.assertEqual(native._research_policy['source_workspace'], 'source0')
            self.assertEqual(native._research_policy['materials'], ['source1'])
            self.assertEqual(native._research_policy['execution_boundary']['code_task'], task)
            self.assertEqual(native._research_policy['queries'], ['method conditions'])
            self.assertEqual(native._execution_contract['conditions'][0]['source'], 'implementation')
            self.assertEqual(native._process_limits, {'process_invocations': 3, 'process_wall_seconds': 15})
            self.assertEqual((vars(args), execution, research, limits), before)
            for interaction in ('assisted', 'checkpoints'):
                args.interaction = interaction
                args.task_kind = 'measurement'
                args.selected_idea_id = 'idea-2'
                args._explicit_resume_destinations.add('selected_idea_id')
                with patch('simple_ar.cli.main._run_native_start') as run, patch('subprocess.run') as process:
                    self.assertTrue(migrate_exact_code_creation(args, execution=execution,
                        request=args.topic, outputs=('experiments', 'summary'), sources=sources, limits=limits,
                        root=root / interaction, research_config={**research, 'interaction': interaction,
                            'research_selected_idea_id': 'idea-2'}))
                    native = run.call_args.args[0]
                    self.assertEqual(native._research_policy['interaction'], interaction)
                    self.assertTrue(native._research_policy['require_design'])
                    self.assertTrue(native._research_policy['require_summary'])
                    self.assertEqual(native._research_policy['selected_idea_id'], 'idea-2')
                    self.assertEqual(native._research_policy['execution_boundary']['code_task'], task)
                    self.assertFalse(native.approve_execution)
                    process.assert_not_called()
            # Explicit old baselines use the same mounted evidence projection
            # in CodeTask, not a second legacy creation or a prepared-source run.
            args.interaction = 'checkpoints'
            args.config = str(root / 'task.toml')
            baseline_file = root / 'baseline.json'
            baseline_file.write_text('{}', encoding='utf-8')  # Domain tests own canonical validation.
            for code_kind in ('existing_project', 'greenfield'):
                declared = {**execution, 'baseline_policy': 'reuse', 'baseline_ref': 'baseline.json',
                    'command': ['python', 'measure.py'],
                    'baseline': {'command': ['python', 'old_measure.py']},
                    'code_task': {**task, 'kind': code_kind,
                        **({'code_root': None, 'workspace_mode': 'empty'} if code_kind == 'greenfield' else {})}}
                if code_kind == 'greenfield':
                    declared['cwd'] = str(root)
                    declared.pop('baseline')
                snapshot = deepcopy(declared)
                with self.subTest(reuse_code_kind=code_kind), \
                        patch('simple_ar.cli.main._run_native_start') as run, \
                        patch('simple_ar.cli.start.print_line'):
                    self.assertTrue(migrate_exact_code_creation(args, execution=declared,
                        request='Implement using a retained baseline', outputs=('experiments', 'summary'),
                        sources=sources, limits=limits, root=root / code_kind,
                        research_config={**research, 'interaction': 'checkpoints',
                                         'research_selected_idea_id': 'idea-2'}))
                native = run.call_args.args[0]
                baseline, candidate = native._execution_contract['conditions']
                self.assertEqual(native.source[-1], baseline_file)
                self.assertEqual(baseline['reuse_ref'], f'source{len(native.source)-1}')
                self.assertEqual(baseline['source'], 'declared')
                self.assertNotIn('before_implementation', baseline)
                self.assertEqual(baseline['execution']['command'],
                    ['python', 'measure.py' if code_kind == 'greenfield' else 'old_measure.py'])
                self.assertEqual(candidate['execution']['command'], [sys.executable, 'measure.py'])
                self.assertEqual(candidate['source'], 'implementation')
                self.assertEqual(native._process_limits, {'process_invocations': 3, 'process_wall_seconds': 15})
                self.assertFalse(native.approve_execution)
                self.assertEqual(native._research_policy['interaction'], 'checkpoints')
                self.assertTrue(native._research_policy['require_summary'])
                self.assertEqual(native._research_policy['execution_boundary']['baseline_ref'], baseline['reuse_ref'])
                self.assertEqual(declared, snapshot)
            self.assertEqual((project / 'check.py').read_text(), 'pass\n')
            self.assertEqual(paper.read_text(), 'Source method and conditions')
            with patch('simple_ar.cli.main._run_native_start') as run:
                # An existing run is adoption, not a fresh source preparation.
                self.assertFalse(migrate_exact_code_creation(args,
                    execution={**execution, 'code_task': {'run_dir': str(root / 'existing-run')}},
                    request='Adopt an existing run', outputs=('experiments',), sources=sources,
                    limits=limits, root=root, research_config=research))
                for declaration in (
                        {key: value for key, value in execution.items() if key != 'baseline_policy'},
                        {**execution, 'baseline_policy': 'reuse'},
                        {**execution, 'unmapped_execution_control': True}):
                    self.assertFalse(migrate_exact_code_creation(args, execution=declaration,
                        request='Retain open or historical execution', outputs=('experiments',), sources=sources,
                        limits=limits, root=root, research_config=research))
                args._explicit_resume_destinations.add('unmapped_control')
                self.assertFalse(migrate_exact_code_creation(args, execution=execution,
                    request='Retain unmapped explicit controls', outputs=('experiments',), sources=sources,
                    limits=limits, root=root, research_config=research))
                run.assert_not_called()

    def test_prepared_reproduction_uses_existing_config_and_preserves_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = root / "source with spaces.md"
            paper.write_text("# Published conclusion\n", encoding="utf-8")
            config = self.prepare("--kind", "reproduction", "--goal", "Check a published conclusion",
                "--document", str(paper), "--hypothesis", "Published claim, not a new method",
                "--dataset", "User-prepared adapted data", "--expected-outcome", "Compare coverage with 0.9",
                "--metric", "coverage", "--metric", "mc_error", "--cwd", directory, "--timeout-sec", "12",
                "--output-files", '{"raw":"measurements.json","setup":"summary.json"}',
                "--metric-sources", '{"coverage":{"output":"raw","path":["scores",0,"coverage"]}}',
                "--max-cited-sources", "1", "--output-root", str(root / "runs"), "--prepare-only",
                "--command", "python", "a script.py", "--label", "one value")
            values = research_defaults(["research-session", "--config", str(config)])
            self.assertEqual(values["task_kind"], "reproduction")
            self.assertEqual(values["outputs"], ["experiments", "report"])
            self.assertEqual(values["command_argv"], ["python", "a script.py", "--label", "one value"])
            self.assertEqual(values["cwd"], str(root.resolve()))
            self.assertEqual(values["execution_details"]["baseline_policy"], "skip")
            self.assertEqual(values["execution_details"]["protocol"]["dataset"], "User-prepared adapted data")
            self.assertEqual(values["process_invocations"], 1)
            self.assertEqual(values["process_wall_seconds"], 12)
            self.assertEqual(values["report_template"], "reproduction")
            self.assertEqual(values["report_draft_scope"], "document")
            self.assertEqual(values["report_review_scope"], "document")
            self.assertEqual(values["report_outline_strategy"], "adaptive")
            self.assertEqual(values["execution_details"]["output_files"], {"raw": "measurements.json", "setup": "summary.json"})
            self.assertEqual(values["execution_details"]["metric_sources"], {"coverage": {"output": "raw", "path": ["scores", 0, "coverage"]}})
            self.assertTrue(values["report_document_review"])
            self.assertEqual(values["report_max_cited_sources"], 1)
            self.assertNotIn("total_tokens", values)
            self.assertFalse(values["research_allow_pdf_download"])
            self.assertFalse((config.parent / "code_task.toml").exists())
            with patch('simple_ar.cli.start.migrate_exact_code_creation') as migrate:
                adapter = self.prepare("--kind", "reproduction", "--goal", "Export author results without changing the method",
                    "--document", str(paper), "--hypothesis", "Published claim", "--dataset", "Fixed data",
                    "--expected-outcome", "Measure coverage", "--metric", "coverage", "--project", directory,
                    "--data-path", directory, "--allow", "adapter.py", "--validate", "python tests/check_adapter.py",
                    "--output-root", str(root / "adapted-runs"), "--prepare-only", "--command", "python", "adapter.py")
            migrate.assert_not_called()  # Preparation still emits the existing structured TOML.
            prepared = research_defaults(["research-session", "--config", str(adapter)])
            self.assertEqual(prepared["command_argv"], ["python", "adapter.py"])
            self.assertEqual(prepared["process_invocations"], 3)
            self.assertEqual(load_code_task_init_options(config_path=adapter.parent / "code_task.toml").benchmark_command,
                             "python tests/check_adapter.py")
            self.assertIn("do not alter methods", (adapter.parent / "task.md").read_text())

    def test_invalid_reproduction_and_cross_function_options_create_no_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            paper = Path(directory) / "paper.md"
            paper.write_text("source", encoding="utf-8")
            output = Path(directory) / "runs"
            base = ["--kind", "reproduction", "--goal", "Check", "--document", str(paper),
                "--hypothesis", "Claim", "--dataset", "Data", "--expected-outcome", "Criterion",
                "--metric", "coverage", "--output-root", str(output), "--prepare-only"]
            for flags in (["--sources", "search"], ["--fulltext"], ["--timeout-sec", "0"],
                          ["--cwd", str(Path(directory) / "missing")], ["--metric", "coverage"],
                          ["--project", str(Path(directory) / "missing-project")],
                          ["--output-files", '{"raw":"../outside.json"}'], ["--output-files", '[]']):
                with self.subTest(flags=flags), self.assertRaises(ValueError):
                    self.prepare(*base, *flags, "--command", "python", "run.py")
                self.assertFalse(output.exists())
            for command in ([], [""]):
                with self.subTest(command=command), self.assertRaises(ValueError):
                    self.prepare(*base, "--command", *command)
                self.assertFalse(output.exists())
            with self.assertRaisesRegex(ValueError, "Missing input"):
                self.prepare("--kind", "reproduction", "--goal", "Check", "--document", str(paper),
                    "--hypothesis", "Claim", "--output-root", str(output), "--prepare-only")
            with self.assertRaisesRegex(ValueError, "require --kind reproduction"):
                self.prepare("--kind", "survey", "--goal", "Read", "--sources", "search", "--prepare-only",
                    "--output-root", str(output), "--command", "python", "run.py")
            self.assertFalse(output.exists())

    def test_interactive_reproduction_asks_for_protocol_not_a_research_algorithm(self):
        with tempfile.TemporaryDirectory() as directory:
            paper = Path(directory) / "paper.md"
            paper.write_text("source", encoding="utf-8")
            args = build_parser().parse_args(["start", "--kind", "reproduction", "--goal", "Check",
                "--document", str(paper), "--environment", "current", "--output-root", directory, "--prepare-only"])
            args._explicit_start_destinations = {'environment'}
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=[
                    "Published claim", "Existing data", "Value near 0.9", '["python", "run.py"]', "coverage",
                    ]), contextlib.redirect_stdout(io.StringIO()):
                config = prepare_start(args)
            values = research_defaults(["research-session", "--config", str(config)])
            self.assertEqual(values["command_argv"], ["python", "run.py"])
            self.assertEqual(values["timeout_sec"], 300)
            self.assertEqual(values["metric"], ["coverage"])
            self.assertTrue(values["research_materials_only"])


if __name__ == "__main__":
    unittest.main()
