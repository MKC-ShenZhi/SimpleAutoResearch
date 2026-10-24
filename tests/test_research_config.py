import tempfile
import unittest
import json
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from simple_ar.cli.parser import build_parser
from simple_ar.cli.research_config import FIELDS, report_settings, research_defaults


class ReportDocumentArgumentsTests(unittest.TestCase):
    def test_project_material_config_roundtrip_uses_common_creation_projection(self):
        from simple_ar.cli.main import main
        from simple_ar.cli.start import native_creation_options
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / 'original'
            root.mkdir()
            project = root / 'project'
            project.mkdir()
            note = root / 'note.md'
            note.write_text('Existing results', encoding='utf-8')
            config = root / 'task.toml'
            with patch('sys.stdin.isatty', return_value=False), redirect_stdout(io.StringIO()):
                main(['start', 'Combine source and existing observations', '--project', str(project),
                      '--material', str(note), '--model', 'profile:science', '--max-processes', '0',
                      '--write-task-config', str(config)])
            # A relocated configuration resolves role paths in its new home;
            # explicit CLI material replaces that role, while other roles stay.
            config.write_text('[task]\nrequest="Combine source and observations"\n'
                'project="project"\nmaterial=["missing-old-note.md"]\nmodel="profile:science"\n'
                'max_processes=0\nmax_turns=4\nmax_tokens=500\n', encoding='utf-8')
            moved = base / 'moved'
            shutil.copytree(root, moved)
            config, project, note = moved / 'task.toml', moved / 'project', moved / 'note.md'
            with patch('sys.stdin.isatty', return_value=False), \
                    patch('simple_ar.cli.main._run_native_start') as run:
                main(['start', '--task-config', str(config), '--material', str(note),
                      '--max-tokens', '900', '--prepare-only'])
            args = run.call_args.args[0]
            options = native_creation_options(args)
            self.assertEqual(set(options['sources'].values()), {project, note})
            self.assertEqual(options['model'], 'profile:science')
            self.assertEqual(options['max_tokens'], 900)
            self.assertEqual(options['max_turns'], 4)
            self.assertEqual(options['process_limits'], {'process_invocations': 0})
            self.assertFalse(options['allow_execution'])

    def test_native_task_config_export_edit_create_and_explicit_override(self):
        from simple_ar.cli.main import main
        from simple_ar.app.tool_application import ToolApplication
        from simple_ar.cli.task_options import apply_task_config, native_process_limits
        import venv
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            environment = root / 'project-environment'
            venv.EnvBuilder(with_pip=False, symlinks=sys.platform != 'win32').create(environment)
            interpreter = environment / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
            config = root / 'task.toml'
            materials = root / 'materials'
            materials.mkdir()
            (materials / 'notes.md').write_text('Saved measurements, not new experiments.', encoding='utf-8')
            with patch('simple_ar.integrations.llm.LLMClient.from_env', side_effect=AssertionError('No external calls')), \
                    patch('sys.stdin.isatty', return_value=False), redirect_stdout(io.StringIO()):
                main(['start', 'Inspect results and explain limitations', '--source', str(materials),
                      '--max-processes', '0', '--max-process-seconds', '12',
                      '--project-python', str(interpreter),
                      '--write-task-config', str(config)])
                self.assertTrue(config.is_file())
                import tomllib
                self.assertEqual(tomllib.loads(config.read_text())['task']['project_python'], str(interpreter))
                with self.assertRaises(SystemExit):
                    main(['start', '--write-task-config', str(config)])
            # Relative inputs resolve against the configuration, CLI budget overrides it.
            config.write_text('[task]\nrequest="Inspect nested materials"\nsource=["materials"]\n'
                'project_python=' + json.dumps(str(interpreter.relative_to(root))) + '\n'
                'max_turns=4\nmax_tokens=500\nmax_processes=0\nmax_process_seconds=12\n', encoding='utf-8')
            args = build_parser().parse_args(['start', '--task-config', str(config), '--max-tokens', '900'])
            args._explicit_start_destinations = {'task_config', 'max_tokens'}
            apply_task_config(args)
            self.assertEqual(args.max_tokens, 900)
            self.assertEqual(args.source, [materials.resolve()])
            self.assertEqual(args.project_python, interpreter)
            app = ToolApplication.create(root / 'session', args.request, model=None,
                sources={'source0': args.source[0]}, max_turns=args.max_turns, max_tokens=args.max_tokens,
                process_limits=native_process_limits(args), project_python=args.project_python)
            loaded = ToolApplication.load(app.session.store.root)
            self.assertEqual(loaded.ledger.limits['process_invocations'], 0)
            self.assertEqual(loaded.ledger.limits['process_wall_seconds'], 12)
            self.assertEqual(loaded.state['config']['sources']['source0'], str(materials.resolve()))
            self.assertFalse(loaded.ledger.entries)
            self.assertEqual(loaded.state['config']['project_python'], str(interpreter))
            # The public roundtrip must preserve actual venv behavior, not
            # merely its displayed path while selecting the base interpreter.
            probe = ToolApplication.create(root / 'environment-check', 'Observe configured interpreter',
                model=None, sources={}, project_python=args.project_python,
                process_limits={'process_invocations': 1, 'process_wall_seconds': 10})
            observed = probe.workspace.call('run_process', {'argv': ['python', '-c',
                'import sys; print(sys.prefix)'], 'timeout_sec': 5}, identity='roundtrip-environment')
            self.assertTrue(observed.ok, observed.error)
            self.assertEqual(observed.data['returncode'], 0, observed.data.get('stderr'))
            self.assertEqual(observed.data['stdout'].strip(), str(environment))
            override = build_parser().parse_args(['start', '--task-config', str(config),
                '--project-python', sys.executable])
            override._explicit_start_destinations = {'task_config', 'project_python'}
            apply_task_config(override)
            self.assertEqual(override.project_python, Path(sys.executable))
            for bad in ('max_turns=true', 'max_process_seconds=inf', 'api_key="not-a-task-setting"'):
                config.write_text('[task]\n' + bad + '\n', encoding='utf-8')
                args = build_parser().parse_args(['start', '--task-config', str(config)])
                args._explicit_start_destinations = {'task_config'}
                with self.assertRaises(ValueError):
                    apply_task_config(args)

    def test_public_cli_article_and_sections_control_contract(self):
        from simple_ar.cli.research_config import report_writing_method

        parser = build_parser()
        for command, required, prefix in (
                ('research-session', ['--topic', 'Explain supplied evidence'], 'report-'),
                ('research-report', ['--session-root', 'run', '--model', 'fixture'], '')):
            for flags in ([], [f'--{prefix}reviewer', 'llm'],
                          [f'--no-{prefix}document-review', '--max-review-iterations', '0']):
                with self.subTest(command=command, flags=flags):
                    from simple_ar.cli.main import _explicit_session_option_destinations
                    args = parser.parse_args([command, *required, *flags])
                    args._explicit_resume_destinations = _explicit_session_option_destinations(
                        parser, [command, *required, *flags], set(), command=command)
                    settings = report_settings(args)
                    expected = set() if not flags else ({'reviewer'} if 'llm' in flags
                                                        else {'document_review', 'max_review_iterations'})
                    original = settings.copy()
                    with patch('simple_ar.report.writing.resolve_report_writing_config',
                               return_value=('sections', original)) as owner:
                        self.assertEqual(report_writing_method(args, settings), ('sections', original))
                    owner.assert_called_once_with(settings, method=None, explicit_fields=expected)
                    self.assertEqual(settings, original)

    def test_toml_report_controls_forward_explicit_fields_to_writing_owner(self):
        from simple_ar.cli.research_config import report_writing_method

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'task.toml'
            path.write_text('[task]\ngoal="Explain supplied evidence"\n[report]\n'
                'reviewer="disabled"\ndocument_review=false\nmax_review_iterations=0\n'
                'max_cited_sources=2\n', encoding='utf-8')
            argv = ['research-session', '--config', str(path)]
            explicit = set()
            defaults = research_defaults(argv, explicit_destinations=explicit)
            args = build_parser(research_defaults=defaults).parse_args(argv)
            args._explicit_resume_destinations = explicit
            settings = report_settings(args)
            self.assertEqual({key: settings[key] for key in (
                'reviewer', 'document_review', 'max_review_iterations', 'max_cited_sources')},
                {'reviewer': 'disabled', 'document_review': False,
                 'max_review_iterations': 0, 'max_cited_sources': 2})
            with patch('simple_ar.report.writing.resolve_report_writing_config',
                       return_value=('article', settings)) as owner:
                self.assertEqual(report_writing_method(args, settings), ('article', settings))
            owner.assert_called_once_with(settings, method=None, explicit_fields={
                'reviewer', 'document_review', 'max_review_iterations', 'max_cited_sources'})

    def test_report_projection_uses_existing_field_owner_for_both_commands(self):
        from argparse import Namespace

        values = {"template": "analysis_report", "reviewer": "llm", "outline_strategy": "adaptive",
                  "data_tables": "full", "max_review_iterations": 0, "document_review": False,
                  "review_scope": "section", "draft_scope": "section",
                  "max_document_review_prompt_chars": 0, "max_section_tokens": 0,
                  "max_cited_sources": 0, "figures": {}}
        for command in ("research-session", "research-report"):
            with self.subTest(command=command):
                destinations = {key if command == "research-report" else destination: values[key]
                    for key, (destination, _) in FIELDS["report"].items() if key != "writing_method"}
                args = Namespace(command=command, report_writing_method="article", **destinations)
                original = vars(args).copy()
                self.assertEqual(report_settings(args), values)
                self.assertEqual(report_settings(args, explicit_destinations=set()), {})
                self.assertEqual(report_settings(args, explicit_destinations=set(destinations)), values)
                self.assertEqual(vars(args), original)

    def test_report_resume_does_not_promote_defaults_or_omit_explicit_false_and_zero(self):
        from types import SimpleNamespace
        from simple_ar.cli.main import _report_config_overrides

        args = SimpleNamespace(command="research-session", report_template="experiment",
            report_document_review=False, max_review_iterations=0, max_section_tokens=0,
            report_max_cited_sources=0, report_figures={},
            _explicit_resume_destinations={"report_document_review", "max_section_tokens", "report_figures"})
        saved = {"template": "analysis_report", "document_review": True, "max_review_iterations": 2,
                 "max_section_tokens": 2000, "figures": {"enabled": True}}
        app = SimpleNamespace(services=SimpleNamespace(config={"report": saved.copy()}))
        self.assertEqual(_report_config_overrides(args, app),
                         {"document_review": False, "max_section_tokens": 0, "figures": {}})
        self.assertEqual(app.services.config["report"], saved)

    def test_report_projection_ignores_missing_or_none_but_rejects_negative_caps(self):
        from argparse import Namespace

        self.assertEqual(report_settings(Namespace(command="research-report", template=None)), {})
        for command in ("research-session", "research-report"):
            for key in ("max_section_tokens", "max_document_review_prompt_chars"):
                with self.subTest(command=command, key=key):
                    args = Namespace(command=command, **{key: -1})
                    with self.assertRaisesRegex(SystemExit, "cannot be negative"):
                        report_settings(args)
                    self.assertEqual(report_settings(args, explicit_destinations=set()), {})

    def test_negative_report_cap_is_rejected_before_model_setup_or_session_writes(self):
        from simple_ar.cli.main import main

        for command, required in (("research-session", ["--topic", "Scope", "--model", "fixture"]),
                                  ("research-report", ["--session-root", "unused", "--model", "fixture"])):
            with self.subTest(command=command), \
                 patch("simple_ar.cli.main._optional_research_llm_client") as setup, \
                 patch("simple_ar.app.research_application.create_session") as create:
                with self.assertRaisesRegex(SystemExit, "cannot be negative"):
                    main([command, *required, "--max-section-tokens", "-1"])
                setup.assert_not_called()
                create.assert_not_called()

    def test_shared_controls_keep_defaults_names_and_explicit_disable(self):
        parser = build_parser()
        for command, prefix, required in (("research-session", "report-", ["--topic", "test"]),
                                         ("research-report", "", ["--session-root", "run", "--model", "fixture"])):
            with self.subTest(command=command):
                argv = [command, *required]
                defaults = vars(parser.parse_args(argv))
                for name in ("document_review", "review_scope", "draft_scope", "outline_strategy", "data_tables"):
                    self.assertIsNone(defaults[prefix.replace("-", "_") + name])
                self.assertIsNone(defaults["max_document_review_prompt_chars"])
                args = vars(parser.parse_args([*argv, f"--no-{prefix}document-review",
                    f"--{prefix}review-scope", "document", f"--{prefix}draft-scope", "document",
                    f"--{prefix}outline-strategy", "adaptive", f"--{prefix}data-tables", "full",
                    "--max-document-review-prompt-chars", "0"]))
                self.assertIs(args[prefix.replace("-", "_") + "document_review"], False)
                for name, expected in (("review_scope", "document"), ("draft_scope", "document"),
                                       ("outline_strategy", "adaptive"), ("data_tables", "full")):
                    self.assertEqual(args[prefix.replace("-", "_") + name], expected)
                self.assertEqual(args["max_document_review_prompt_chars"], 0)


class DocumentReviewCapacityConfigTests(unittest.TestCase):
    def test_capacity_roundtrips_task_config_cli_and_resume(self):
        from simple_ar.cli.main import _report_config_overrides
        from types import SimpleNamespace
        from simple_ar.report.schema import ReportRuntimeConfig
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.toml"
            path.write_text('[task]\ngoal="Review supplied materials"\n[report]\nmax_document_review_prompt_chars=0\n')
            argv = ["research-session", "--config", str(path)]
            explicit = set()
            defaults = research_defaults(argv, explicit_destinations=explicit)
            parser = build_parser(research_defaults=defaults)
            args = parser.parse_args(argv)
            args._explicit_resume_destinations = explicit
            app = SimpleNamespace(services=SimpleNamespace(config={"report_config": {}}))
            self.assertEqual(_report_config_overrides(args, app)["max_document_review_prompt_chars"], 0)
            self.assertEqual(parser.parse_args([*argv, "--max-document-review-prompt-chars", "120000"]).max_document_review_prompt_chars, 120000)
            report_args = parser.parse_args(["research-report", "--session-root", "run", "--model", "fixture", "--max-document-review-prompt-chars", "100000"])
            report_args._explicit_resume_destinations = {"max_document_review_prompt_chars"}
            self.assertEqual(_report_config_overrides(report_args, app)["max_document_review_prompt_chars"], 100000)
            path.write_text(path.read_text().replace("=0", "=-1"))
            with self.assertRaisesRegex(ValueError, "max_document_review_prompt_chars"):
                research_defaults(argv)
        self.assertEqual(ReportRuntimeConfig.model_validate({}).max_document_review_prompt_chars, 0)


class ResearchConfigTests(unittest.TestCase):
    def _historical_summary(self, session, papers, *, goal, limits=None):
        """Seed the retained historical owner, not today's public new route."""
        from simple_ar.app.research_application import ResearchApplication, ResearchApplicationServices
        from simple_ar.research.workflow_contracts import ResearchBrief
        app = ResearchApplication.create(ResearchBrief(request_text=goal, objective=goal,
            intents=('research',), requested_outputs=('summary',), asset_requests=tuple({
                'locator': str(path), 'kind': 'file', 'role': 'paper', 'mutability': 'read_only',
                'allowed_uses': ['read', 'reference']} for path in papers)), root=session,
            services=ResearchApplicationServices(max_results=10, max_chunks=300,
                budget_limits=limits or {}, max_attempts=20,
                config={'research_sources': ['local_files'], 'research_materials_only': True,
                    'interaction': 'autonomous', 'report_writing_method': 'sections',
                    'report': {'mode': 'research_only', 'template': 'survey', 'reviewer': 'llm'}}))
        self.assertEqual(app.advance(max_actions=20).status, 'completed')
        return app

    def test_named_experiment_data_resolves_relative_config_paths_and_explicit_cli_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'research.toml'
            config.write_text('[task]\ngoal="Check result"\nkind="reproduction"\noutputs=["experiments"]\n'
                              '[assets]\ndata=["shared/train.npz", "shared/test"]\n')
            argv = ['research-session', '--config', str(config)]
            defaults = research_defaults(argv)
            self.assertEqual(defaults['data_path'], [str((root / 'shared/train.npz').resolve()), str((root / 'shared/test').resolve())])
            args = build_parser(research_defaults=defaults).parse_args(argv)
            self.assertEqual([str(path) for path in args.data_path], defaults['data_path'])
            overridden = research_defaults([*argv, '--data-path', 'other.npz'])
            self.assertNotIn('data_path', overridden)
            config.write_text('[task]\ngoal="Survey"\nkind="survey"\n[assets]\ndata=["shared"]\n')
            args = build_parser(research_defaults=research_defaults(argv)).parse_args(argv)
            from simple_ar.cli.research_config import validate_session_arguments
            with self.assertRaisesRegex(SystemExit, 'belongs to experiment inputs'):
                validate_session_arguments(args)

    def test_review_scope_roundtrips_config_session_and_report_resume(self):
        from simple_ar.cli.main import _report_config_overrides
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.toml"
            path.write_text('[task]\ngoal="Organize supplied evidence"\n[report]\ndocument_review=true\nreview_scope="document"\n')
            argv = ["research-session", "--config", str(path)]
            explicit = set()
            defaults = research_defaults(argv, explicit_destinations=explicit)
            parser = build_parser(research_defaults=defaults)
            args = parser.parse_args(argv)
            args._explicit_resume_destinations = explicit
            app = SimpleNamespace(services=SimpleNamespace(config={"report": {}}))
            self.assertEqual(_report_config_overrides(args, app)["review_scope"], "document")
            self.assertEqual(parser.parse_args([*argv, "--report-review-scope", "section"]).report_review_scope, "section")
            report = parser.parse_args(["research-report", "--session-root", "run", "--model", "fixture",
                "--document-review", "--review-scope", "document"])
            report._explicit_resume_destinations = {"document_review", "review_scope"}
            self.assertEqual(_report_config_overrides(report, app), {"document_review": True, "review_scope": "document"})
            path.write_text(path.read_text().replace('"document"', '"unknown"'))
            with self.assertRaisesRegex(ValueError, "review_scope"):
                research_defaults(argv)

    def test_invalid_document_first_combination_fails_before_model_setup(self):
        from simple_ar.cli.main import main
        for flags, message in ((['--report-review-scope', 'document'], 'requires document_review'),
                               (['--report-writing-method', 'article', '--report-outline-strategy', 'adaptive'],
                                'cannot execute.*outline_strategy')):
            with self.subTest(flags=flags), \
                 patch('simple_ar.cli.main._optional_research_llm_client') as setup, \
                 patch('simple_ar.app.research_application.create_session') as create:
                with self.assertRaisesRegex(SystemExit, message):
                    main(['research-session', '--topic', 'Explain supplied material', '--model', 'fixture', *flags])
                setup.assert_not_called()
                create.assert_not_called()

    def test_data_tables_share_toml_cli_and_resume_override(self):
        from simple_ar.cli.main import _report_config_overrides
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.toml"
            path.write_text('[task]\ngoal="Explain supplied data"\n[report]\ndata_tables="full"\n')
            argv = ["research-session", "--config", str(path)]
            explicit = set()
            defaults = research_defaults(argv, explicit_destinations=explicit)
            parser = build_parser(research_defaults=defaults)
            args = parser.parse_args(argv)
            args._explicit_resume_destinations = explicit
            app = SimpleNamespace(services=SimpleNamespace(config={"report_config": {}}))
            self.assertEqual(args.report_data_tables, "full")
            self.assertEqual(_report_config_overrides(args, app)["data_tables"], "full")
            self.assertEqual(parser.parse_args([*argv, "--report-data-tables", "linked"]).report_data_tables, "linked")
            self.assertEqual(parser.parse_args(["research-report", "--session-root", "run", "--model", "fixture", "--data-tables", "full"]).data_tables, "full")
            path.write_text(path.read_text().replace('"full"', '"unknown"'))
            with self.assertRaisesRegex(ValueError, "data_tables"):
                research_defaults(argv)
    def test_output_files_are_task_local_validated_attachments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.toml"
            path.write_text('[task]\ngoal="Read measurements"\n[execution]\noutput_files={raw="tables/raw.csv"}\n', encoding="utf-8")
            defaults = research_defaults(["research-session", "--config", str(path)])
            self.assertEqual(defaults["execution_details"]["output_files"], {"raw": "tables/raw.csv"})
            path.write_text(path.read_text().replace("tables/raw.csv", "../outside"), encoding="utf-8")
            with self.assertRaises(ValueError):
                research_defaults(["research-session", "--config", str(path)])

    def test_outline_strategy_uses_canonical_config_and_cli_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.toml"
            path.write_text('[task]\ngoal="Organize supplied evidence"\n[report]\noutline_strategy="adaptive"\n', encoding="utf-8")
            argv = ["research-session", "--config", str(path)]
            explicit = set()
            defaults = research_defaults(argv, explicit_destinations=explicit)
            parser = build_parser(research_defaults=defaults)
            self.assertEqual(parser.parse_args(argv).report_outline_strategy, "adaptive")
            self.assertIn("report_outline_strategy", explicit)
            self.assertEqual(parser.parse_args([*argv, "--report-outline-strategy", "template"]).report_outline_strategy, "template")
            path.write_text(path.read_text(encoding="utf-8").replace('"adaptive"', '"unknown"'), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "outline_strategy"):
                research_defaults(argv)

    def test_direct_measurement_kind_requires_experiments_output(self):
        from simple_ar.cli.main import main
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "measurement.toml"
            path.write_text('[task]\ngoal="Measure supplied command"\nkind="measurement"\noutputs=["experiments"]\n', encoding="utf-8")
            defaults = research_defaults(["research-session", "--config", str(path)])
            args = build_parser(research_defaults=defaults).parse_args(["research-session", "--config", str(path)])
            self.assertEqual((args.task_kind, args.outputs), ("measurement", ["experiments"]))
            path.write_text(path.read_text(encoding="utf-8").replace('outputs=["experiments"]', 'outputs=["report"]'), encoding="utf-8")
            # Combination rules apply once after TOML and CLI are merged,
            # rather than a second, divergent policy in the defaults loader.
            with patch('simple_ar.cli.main._optional_research_llm_client') as client:
                with self.assertRaisesRegex(SystemExit, 'requires --outputs experiments'):
                    main(["research-session", "--config", str(path)])
                client.assert_not_called()

    def test_document_review_is_explicit_and_can_be_disabled_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.toml"
            path.write_text(
                '[task]\ngoal="Review an evidence report"\n'
                '[report]\ndocument_review=true\n', encoding="utf-8",
            )
            argv = ["research-session", "--config", str(path)]
            explicit = set()
            defaults = research_defaults(argv, explicit_destinations=explicit)
            args = build_parser(research_defaults=defaults).parse_args(argv)
            self.assertTrue(args.report_document_review)
            self.assertIn("report_document_review", explicit)
            disabled = build_parser(research_defaults=defaults).parse_args(
                [*argv, "--no-report-document-review"],
            )
            self.assertFalse(disabled.report_document_review)

    def test_resume_without_config_restores_saved_goal_and_keeps_outputs(self):
        from simple_ar.cli.main import main
        from simple_ar.app.tool_application import ToolApplication

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "paper.md").write_text("# Supplied finding\nA bounded fact.\n", encoding="utf-8")
            config = root / "research.toml"
            config.write_text(
                '[task]\ngoal="Summarize a supplied finding"\noutputs=["summary"]\noutput_root="out"\n'
                '[model]\nname=""\n[research]\nmaterials_only=true\nproviders=["local_files"]\n'
                '[assets]\npapers=["paper.md"]\n',
                encoding="utf-8",
            )
            with redirect_stdout(io.StringIO()):
                main(["research-session", "--config", str(config), "--interaction", "autonomous"])
            session = next((root / "out").iterdir())
            before = ToolApplication.load(session)
            original = (before.session.status_snapshot(), before.state, before.ledger.to_dict())
            with redirect_stdout(io.StringIO()):
                main(["research-session", "--session-root", str(session)])
            after = ToolApplication.load(session)
            self.assertEqual(after.session.manifest.status, "completed")
            self.assertEqual((after.session.status_snapshot(), after.state, after.ledger.to_dict()), original)

    def test_toml_interaction_and_decision_reply_reach_the_canonical_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.toml"
            path.write_text(
                '[task]\ngoal="Review a bounded result"\n'
                '[research]\ninteraction="assisted"\n'
                '[continuation]\ndecision_id="0123456789abcdef"\n'
                'decision_response="revise"\ndecision_guidance="Keep the comparison local"\n',
                encoding="utf-8",
            )
            explicit = set()
            defaults = research_defaults(
                ["research-session", "--config", str(path)],
                explicit_destinations=explicit,
            )
            args = build_parser(research_defaults=defaults).parse_args(
                ["research-session", "--config", str(path)],
            )
            self.assertEqual(args.interaction, "assisted")
            self.assertEqual(args.decision_id, "0123456789abcdef")
            self.assertEqual(args.decision_response, "revise")
            self.assertEqual(args.decision_guidance, "Keep the comparison local")
            self.assertTrue({"interaction", "decision_id", "decision_response", "decision_guidance"} <= explicit)

    def test_config_accepts_compact_seed_protocol_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.toml"
            path.write_text(
                '[task]\ngoal="Run two different seeds for the seed fixture"\noutputs=["experiments"]\n'
                f'[execution]\ncommand={json.dumps([sys.executable, "measure.py"])}\ncwd="."\n'
                'timeout_sec=5\nseed_count=2\nseed_flag="--seed"\nbaseline_policy="skip"\n',
                encoding="utf-8",
            )
            defaults = research_defaults(["research-session", "--config", str(path)])
            self.assertEqual(defaults["execution_details"]["seed_count"], 2)
            self.assertEqual(defaults["execution_details"]["seed_flag"], "--seed")
            self.assertEqual(defaults["execution_details"]["baseline_policy"], "skip")

    def test_config_preserves_reused_baseline_paths_as_scalar_or_list(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'research.toml'
            for references in ('prior result.json', ['seed-4.json', 'archive/../seed-9.json']):
                with self.subTest(references=references):
                    path.write_text('[task]\ngoal="Reuse supplied baseline evidence"\n'
                        '[execution]\nbaseline_policy="reuse"\n'
                        f'baseline_ref={json.dumps(references)}\nseeds=[4,9]\nseed_flag="--seed"\n'
                        'baseline={command=["python", "baseline.py"]}\n',
                        encoding='utf-8')
                    original = path.read_bytes()
                    argv = ['research-session', '--config', str(path)]
                    with patch('simple_ar.app.research_execution.normalize_execution_config',
                               side_effect=AssertionError('Declarations must not expand execution')):
                        defaults = research_defaults(argv)
                    self.assertEqual(defaults['execution_details'], {
                        'baseline_policy': 'reuse', 'baseline_ref': references,
                        'baseline': {'command': ['python', 'baseline.py']},
                        'seeds': [4, 9], 'seed_flag': '--seed'})
                    args = build_parser(research_defaults=defaults).parse_args(argv)
                    self.assertEqual(args.execution_details, defaults['execution_details'])
                    self.assertNotIn('command_argv', defaults)
                    self.assertEqual(path.read_bytes(), original)

    def test_config_pair_protocol_real_processes_and_completed_resume(self):
        import hashlib
        from simple_ar.cli.main import main
        from simple_ar.app.tool_application import ToolApplication
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "paper.md").write_text("# Replay learning\nMemory stores previous examples.", encoding="utf-8")
            (root / "measure.py").write_text(
                'import sys\nfrom pathlib import Path\np=Path("calls.txt")\n'
                'with p.open("a") as f: f.write(" ".join(sys.argv[1:])+"\\n")\n'
                'print("accuracy: 0.7" if sys.argv[1]=="candidate" else "accuracy: 0.6")\n', encoding="utf-8")
            source = ('[task]\ngoal="Replay learning"\noutputs=["experiments"]\noutput_root="out"\n'
                      '[model]\nname=""\n[research]\nproviders=["local_files"]\n[assets]\npapers=["paper.md"]\n'
                      '[budget]\nprocess_invocations=4\nprocess_wall_seconds=80\n'
                      '[execution]\ncwd="."\ntimeout_sec=10\nprimary_metric="accuracy"\n'
                      '[execution.protocol]\ncontract_id="fixture-pairs"\n'
                      'dataset_refs=[{asset_id="fixture"}]\nsplit_spec={name="fixed"}\n'
                      'metric_specs=[{name="accuracy",unit="fraction"}]\ncomparison_conditions={name="fixed"}\n'
                      'protected_assets=[{asset_id="evaluator",path="measure.py"}]\n')
            for seed in (0, 1):
                source += '\n[[execution.pairs]]\nseed=' + str(seed) + '\n'
                for role in ("baseline", "candidate"):
                    source += role + '_command=' + json.dumps([sys.executable, "measure.py", role, str(seed)]) + '\n'
            path = root / "research.toml"
            path.write_text(source, encoding="utf-8")
            argv = ["research-session", "--config", str(path)]
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(SystemExit, "paused"):
                main(argv)
            session = next((root / "out").iterdir())
            app = ToolApplication.load(session)
            self.assertEqual(app.session.manifest.status, "paused")
            self.assertEqual(app.sources.policy.interaction, "checkpoints")
            gate = app.sources.pending_decision()
            self.assertEqual(gate["proposal_kind"], "caller_execution_protocol")
            self.assertEqual(len(app.state["config"]["execution_contract"]["conditions"]), 4)
            self.assertTrue(any((attempt / "research_summary.md").is_file()
                                for attempt in (session / "attempts").iterdir()))
            self.assertFalse((root / "calls.txt").exists())
            resume = ["start", "--resume-task", str(session), "--yes"]
            with redirect_stdout(io.StringIO()):
                main(resume + ["--decision-id", gate["decision_id"],
                             "--decision-response", "accept"])
            self.assertFalse((root / "calls.txt").exists())
            self.assertFalse(ToolApplication.load(session).ledger.entries)
            tokens = set()
            for index in range(4):
                app = ToolApplication.load(session)
                self.assertEqual(app.session.manifest.status, "paused")
                call = app.state["pending"]["call"]
                self.assertEqual(call["function"]["name"], "measure_experiment")
                token = app.approval_token(call)
                self.assertNotIn(token, tokens)
                tokens.add(token)
                with redirect_stdout(io.StringIO()):
                    main(resume + ["--approve-execution", token])
                self.assertEqual(len((root / "calls.txt").read_text().splitlines()), index + 1)
            app = ToolApplication.load(session)
            self.assertEqual(app.session.manifest.status, "completed")
            calls = (root / "calls.txt").read_text().splitlines()
            self.assertCountEqual(calls, ["baseline 0", "baseline 1", "candidate 0", "candidate 1"])
            results = [json.loads(p.read_text()) for p in session.glob("attempts/**/results.json")]
            measured = [r for r in results if "measurement" in r]
            self.assertEqual(len(measured), 4)
            self.assertEqual(len({r["measurement"]["measurement_id"] for r in measured}), 4)
            self.assertTrue(all(r["measurement"]["asset_integrity"]["status"] == "observed_unchanged" for r in measured))
            self.assertEqual(sum(entry.actual.get("process_invocations", 0) for entry in app.ledger.entries), 4)
            observations = [app.session.store.read_json(item["result"])
                            for turn in app.state["turns"] for item in turn["tools"]]
            locators = [item["data"]["result_ref"] for item in observations
                        if item.get("tool_name") == "measure_experiment" and item["status"] == "ok"]
            self.assertEqual(len(locators), 4)
            analysis_records = [item["result"] for turn in app.state["turns"] for item in turn["tools"]
                                if app.session.store.read_json(item["result"]).get("tool_name") == "analyze_measurements"]
            self.assertEqual(len(analysis_records), 1)
            self.assertEqual(app.scientific.evidence_requirement(locators, analysis_records), [])
            evidence = app.scientific.report_evidence(locators, [], analysis_records=analysis_records)
            self.assertEqual(len(evidence["measurement_refs"]), 4)
            self.assertEqual(set(evidence["measurement_refs"]), {
                app.workspace.resolve(locator).relative_to(session).as_posix() for locator in locators})
            self.assertFalse(evidence.get("measurement_sources"))  # All four outcomes are original.
            hashes = {ref: hashlib.sha256((session / ref).read_bytes()).hexdigest()
                      for ref in evidence["measurement_refs"]}
            for ref, digest in hashes.items():
                self.assertEqual(evidence["evidence_sha256"][ref], digest)
            attempts = app.session.list_attempts()
            ledger = app.ledger.to_dict()
            with redirect_stdout(io.StringIO()):
                main(resume)
            app = ToolApplication.load(session)
            self.assertEqual(app.session.manifest.status, "completed")
            self.assertEqual(app.session.list_attempts(), attempts)
            self.assertEqual(app.ledger.to_dict(), ledger)
            self.assertEqual({ref: hashlib.sha256((session / ref).read_bytes()).hexdigest()
                              for ref in hashes}, hashes)
            self.assertEqual((root / "calls.txt").read_text().splitlines(), calls)

    def test_resume_attaches_execution_without_research_replay_or_budget_reset(self):
        from simple_ar.cli.main import main
        from simple_ar.app.research_application import ResearchApplication, ResearchApplicationServices, load_session
        from simple_ar.research.workflow_contracts import ResearchBrief
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "paper.md").write_text("# Replay learning\nMemory stores previous examples for classification.", encoding="utf-8")
            (root / "measure.py").write_text('from pathlib import Path\np = Path("calls.txt")\np.write_text(p.read_text() + "x" if p.exists() else "x")\nprint("accuracy: 0.8")\n', encoding="utf-8")
            path = root / "research.toml"
            path.write_text('[task]\ngoal="Replay learning"\noutputs=["experiments"]\noutput_root="out"\n'
                            '[model]\nname=""\n[research]\nproviders=["local_files"]\n[assets]\npapers=["paper.md"]\n'
                            '[budget]\ntotal_tokens=123456\nprocess_invocations=2\nprocess_wall_seconds=60\n', encoding="utf-8")
            argv = ["research-session", "--config", str(path)]
            args = build_parser(research_defaults=research_defaults(argv)).parse_args(argv)
            session = root / "out" / "historical-attach"
            # This resume contract belongs to sessions created by the historical owner.
            app = ResearchApplication.create(ResearchBrief(
                request_text=args.topic, objective=args.topic, intents=("research", "experiment"),
                requested_outputs=tuple(args.outputs), asset_requests=tuple({
                    "locator": paper, "kind": "file", "role": "paper", "mutability": "read_only",
                    "allowed_uses": ["read", "reference"]} for paper in args.local_document)),
                root=session, services=ResearchApplicationServices(
                    max_results=args.max_results, max_chunks=args.max_chunks, idea_limit=args.idea_limit,
                    input_base_dir=root, max_attempts=20,
                    budget_limits={name: getattr(args, name) for name in (
                        "llm_requests", "total_tokens", "process_invocations", "process_wall_seconds")},
                    config={"research_sources": args.providers,
                        "research_max_documents": args.max_results,
                        "research_max_iterations": args.max_research_iterations,
                        "interaction": "checkpoints",
                        "report": {**report_settings(args), "mode": "experiment", "template": args.report_template}}))
            self.assertEqual(app.advance(max_actions=20).status, "paused")
            before = load_session(session).view()
            original_inputs = {
                key: (session / before.state_refs[key].path).read_bytes()
                for key in ("brief", "brief_markdown", "assets")
            }
            path.write_text(path.read_text(encoding="utf-8").replace("123456", "999999") +
                            '\n[execution]\ncommand=[' + json.dumps(sys.executable) + ', "measure.py"]\ncwd="."\ntimeout_sec=20\nprimary_metric="accuracy"\n', encoding="utf-8")
            argv = ["research-session", "--config", str(path), "--session-root", str(session)]
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(SystemExit, "paused"):
                main(argv)
            gate = load_session(session).view().work_plan["interaction"]["decision"]
            self.assertEqual(gate["stage"], "execution_protocol")
            self.assertFalse((root / "calls.txt").exists())
            with redirect_stdout(io.StringIO()):
                main(argv + ["--decision-id", gate["id"], "--decision-response", "accept"])
            app = load_session(session)
            after = app.view()
            self.assertEqual(after.status, "completed")
            for key, ref in before.state_refs.items():
                if key not in {"brief", "assets", "brief_markdown", "runtime_config", "diagnostics", "task_plan", "design", "research_design"}:
                    self.assertEqual(after.state_refs[key], ref)
            for key, content in original_inputs.items():
                self.assertEqual((session / before.state_refs[key].path).read_bytes(), content)
                self.assertNotEqual(after.state_refs[key], before.state_refs[key])
            old_brief = json.loads(original_inputs["brief"])
            new_brief = json.loads((session / after.state_refs["brief"].path).read_text(encoding="utf-8"))
            self.assertEqual(new_brief["revision"], old_brief["revision"] + 1)
            self.assertEqual(new_brief["parent_revision"], old_brief["revision"])
            self.assertEqual(
                {key: value for key, value in new_brief.items() if key not in {"revision", "parent_revision"}},
                {key: value for key, value in old_brief.items() if key not in {"revision", "parent_revision"}},
            )
            self.assertNotEqual(after.state_refs["task_plan"], before.state_refs["task_plan"])
            self.assertEqual(app.budget_ledger.limits["total_tokens"], 123456)
            self.assertEqual((root / "calls.txt").read_text(), "x")
            with redirect_stdout(io.StringIO()):
                main(argv)
            self.assertEqual(load_session(session).view().attempts, after.attempts)
            self.assertEqual((root / "calls.txt").read_text(), "x")

    def test_toml_continuation_adds_material_and_replays_authorization_once(self):
        from simple_ar.cli.main import main
        from simple_ar.app.research_application import load_session
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            papers = [root / f"paper-{index}.md" for index in range(1, 4)]
            for index, paper in enumerate(papers, start=1):
                paper.write_text(f"# Fixture {index}\nA local supplied source on small-memory learning.\n", encoding="utf-8")
            config = root / "research.toml"

            def write_config(selected_papers, *, continuation=True):
                paper_rows = ", ".join(json.dumps(str(path)) for path in selected_papers)
                continuation_config = (
                    '[continuation]\nauthorization_id="review-20260924-1"\n'
                    'reason="Bounded continuation after review."\nadditional_attempts=2\nadditional_no_progress=1\n'
                    'remaining={total_tokens=200, llm_requests=3}\n'
                ) if continuation else ""
                config.write_text(
                    '[task]\ngoal="Summarize the supplied local sources"\noutputs=["summary"]\noutput_root="out"\n'
                    '[model]\nname=""\n[research]\nproviders=["local_files"]\nmaterials_only=true\n'
                    f'[assets]\npapers=[{paper_rows}]\n'
                    '[budget]\ntotal_tokens=100\nllm_requests=2\nprocess_invocations=0\nprocess_wall_seconds=0\n'
                    + continuation_config,
                    encoding="utf-8",
                )

            write_config(papers[:1], continuation=False)
            session = root / 'out' / 'historical'
            initial = self._historical_summary(session, papers[:1], goal='Summarize the supplied local sources',
                limits={'total_tokens': 100, 'llm_requests': 2, 'process_invocations': 0, 'process_wall_seconds': 0})
            initial_view = initial.view()
            original_attempts = initial_view.attempts
            initial_cap = initial.controller.manifest.budget.max_attempts
            from simple_ar.core.budget import BudgetError, BudgetLedger
            initial.budget_ledger.reserve("historical-token-use", {"total_tokens": 7})
            initial.budget_ledger.settle("historical-token-use", {"total_tokens": 7})
            historical_entry = initial.budget_ledger.entries[0].to_dict()

            write_config(papers[:2])
            argv = ["research-session", "--config", str(config), "--session-root", str(session)]
            original_authorize = BudgetLedger.authorize_remaining

            def fail_ledger_once(ledger, *args, **kwargs):
                if kwargs.get("authorization_id") == "review-20260924-1":
                    raise BudgetError("injected manifest-ledger gap")
                return original_authorize(ledger, *args, **kwargs)

            with patch.object(BudgetLedger, "authorize_remaining", new=fail_ledger_once):
                with redirect_stdout(io.StringIO()), self.assertRaisesRegex(
                    SystemExit, "injected manifest-ledger gap",
                ):
                    main(argv)
            after_manifest_only = load_session(session)
            self.assertEqual(after_manifest_only.controller.manifest.budget.max_attempts, initial_cap + 2)
            self.assertEqual(after_manifest_only.budget_ledger.limits["total_tokens"], 100)
            self.assertEqual(after_manifest_only.budget_ledger.entries[0].to_dict(), historical_entry)
            self.assertEqual(after_manifest_only.view().attempts, original_attempts)

            from simple_ar.core.session import SessionController
            with patch.object(
                SessionController, "continue_with_revision",
                side_effect=RuntimeError("injected session continuation failure"),
            ):
                with redirect_stdout(io.StringIO()), self.assertRaisesRegex(
                    SystemExit, "injected session continuation failure",
                ):
                    main(argv)
            after_ledger_only = load_session(session)
            self.assertEqual(after_ledger_only.view().status, "completed")
            self.assertEqual(after_ledger_only.controller.manifest.budget.max_attempts, initial_cap + 2)
            self.assertEqual(after_ledger_only.budget_ledger.limits["total_tokens"], 200)
            self.assertEqual(after_ledger_only.budget_ledger.entries[0].to_dict(), historical_entry)
            self.assertEqual(after_ledger_only.view().attempts, original_attempts)

            with redirect_stdout(io.StringIO()):
                main(argv)
            first_resume = load_session(session)
            first_view = first_resume.view()
            self.assertEqual(first_view.status, "completed")
            self.assertGreater(len(first_view.attempts), len(original_attempts))
            self.assertEqual(first_resume.controller.manifest.budget.attempts, len(first_view.attempts))
            self.assertEqual(first_resume.controller.manifest.budget.max_attempts, initial_cap + 2)
            self.assertEqual(first_resume.budget_ledger.limits["total_tokens"], 200)
            self.assertEqual(first_resume.budget_ledger.limits["llm_requests"], 3)
            self.assertEqual(len(first_resume._local_documents()), 2)

            with redirect_stdout(io.StringIO()):
                main(argv)
            exact_replay = load_session(session)
            self.assertEqual(exact_replay.view().attempts, first_view.attempts)
            self.assertEqual(exact_replay.controller.manifest.budget.max_attempts, initial_cap + 2)
            self.assertEqual(exact_replay.budget_ledger.limits["total_tokens"], 200)

            write_config(papers)
            with redirect_stdout(io.StringIO()):
                main(argv)
            replayed = load_session(session)
            self.assertEqual(replayed.view().status, "completed")
            self.assertEqual(replayed.controller.manifest.budget.max_attempts, initial_cap + 2)
            self.assertEqual(replayed.controller.manifest.budget.continuation_authorizations[
                "review-20260924-1"]["resource_allowances"], {"total_tokens": 200, "llm_requests": 3})
            self.assertEqual(len(replayed._local_documents()), 3)

    def test_config_runs_local_summary_and_persists_budget_without_processes(self):
        from simple_ar.cli.main import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "paper.md").write_text("# Replay learning\nA small memory stores previous examples for classification.", encoding="utf-8")
            path = root / "research.toml"
            path.write_text('[task]\ngoal="Replay learning"\noutputs=["summary"]\noutput_root="out"\n'
                            '[model]\nname=""\n[research]\nproviders=["local_files"]\nuse_fulltext=false\nallow_pdf_download=false\nkeep_raw_pdf=false\n[assets]\npapers=["paper.md"]\n'
                            '[budget]\ntotal_tokens=123456\nprocess_invocations=0\nprocess_wall_seconds=0\n', encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                main(["research-session", "--config", str(path)])
            ledger_path = next((root / "out").glob("*/budget_ledger.json"))
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(ledger["limits"]["total_tokens"], 123456)
            self.assertEqual(ledger["entries"], [])
            from simple_ar.app.tool_application import ToolApplication
            from simple_ar.cli.start import session_materials
            runtime = ToolApplication.load(ledger_path.parent)
            for name in ('require_fulltext', 'allow_pdf_download', 'keep_raw_pdf'):
                self.assertIs(runtime.state['config']['research_policy'][name], False)
            self.assertTrue(session_materials(ledger_path.parent)['summary'])
            path.write_text(path.read_text(encoding="utf-8").replace('outputs=["summary"]', 'outputs=["experiments"]'), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaisesRegex(SystemExit, "status 'paused'"):
                main(["research-session", "--config", str(path)])
            deferred_session, = (candidate.parent for candidate in (root / "out").glob("*/budget_ledger.json")
                                 if candidate != ledger_path)
            deferred = ToolApplication.load(deferred_session)
            self.assertEqual(deferred.session.manifest.status, "paused")
            self.assertTrue(deferred.sources.policy.require_design)
            self.assertIsNone(deferred.state["config"].get("execution_contract"))
            self.assertTrue(any((attempt / "research_summary.md").is_file()
                                for attempt in (deferred_session / "attempts").iterdir()))
            self.assertEqual(session_materials(deferred_session), {})
            self.assertEqual(deferred.ledger.limits["total_tokens"], 123456)
            self.assertFalse(deferred.ledger.entries)
            self.assertIn("not complete", output.getvalue())

    def test_file_paths_and_explicit_cli_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.toml"
            path.write_text('[task]\ngoal="Continual learning"\noutputs=["experiments","report"]\noutput_root="runs"\n'
                            '[budget]\ntotal_tokens=2000000\n[research]\nproviders=["arxiv"]\n'
                            '[assets]\npapers=["paper.md"]\n', encoding="utf-8")
            argv = ["research-session", "--config", str(path), "--total-tokens", "3000000", "--provider", "openalex"]
            args = build_parser(research_defaults=research_defaults(argv)).parse_args(argv)
            self.assertEqual(args.topic, "Continual learning")
            self.assertEqual(args.model, "env")
            self.assertEqual(args.total_tokens, 3000000)
            self.assertEqual(args.providers, ["openalex"])
            self.assertEqual(args.local_document, [str((Path(directory) / "paper.md").resolve())])
            self.assertEqual(args.output_root, str((Path(directory) / "runs").resolve()))
            self.assertEqual(args.outputs, ["experiments", "report"])
            self.assertIsNone(args.command_argv)
            self.assertEqual(args.max_chunks, 300)

    def test_research_iteration_setting_targets_the_consumed_cli_option(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.toml"
            path.write_text(
                '[task]\ngoal="Bounded rounds"\n[research]\nmax_iterations=3\nweb_extract_backend="tavily_basic"\n',
                encoding="utf-8",
            )
            explicit = set()
            argv = ["research-session", "--config", str(path)]
            defaults = research_defaults(argv, explicit_destinations=explicit)
            args = build_parser(research_defaults=defaults).parse_args(argv)
            self.assertEqual(args.max_research_iterations, 3)
            self.assertIn("max_research_iterations", explicit)
            self.assertEqual(args.research_web_extract_backend, "tavily_basic")
            self.assertEqual(build_parser(research_defaults=defaults).parse_args(
                [*argv, "--web-extract-backend", "direct"]).research_web_extract_backend, "direct")

    def test_read_shortlist_limit_is_available_in_task_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.toml"
            path.write_text(
                '[task]\ngoal="Read supplied papers"\n'
                '[research]\nread_max_shortlist=3\n',
                encoding="utf-8",
            )
            explicit = set()
            argv = ["research-session", "--config", str(path)]
            defaults = research_defaults(argv, explicit_destinations=explicit)
            args = build_parser(research_defaults=defaults).parse_args(argv)
            self.assertEqual(args.read_max_shortlist, 3)
            self.assertIn("read_max_shortlist", explicit)

    def test_pdf_page_limit_reaches_source_parser_budget(self):
        from simple_ar.research.documents.extractors import LocalDocumentParser
        from simple_ar.research.sources.base import build_source_plan

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.toml"
            path.write_text(
                '[task]\ngoal="Read a supplied PDF"\n'
                '[research]\nmax_pdf_pages=40\n', encoding="utf-8",
            )
            explicit = set()
            argv = ["research-session", "--config", str(path)]
            defaults = research_defaults(argv, explicit_destinations=explicit)
            args = build_parser(research_defaults=defaults).parse_args(argv)
            self.assertEqual(args.research_max_pdf_pages, 40)
            self.assertIn("research_max_pdf_pages", explicit)
            plan = build_source_plan(
                topic=args.topic, problem_markdown="",
                config={"research_max_pdf_pages": args.research_max_pdf_pages},
                default_query=args.topic, default_max_results=args.max_results,
            )
            self.assertEqual(LocalDocumentParser.from_source_plan(plan).max_pdf_pages, 40)

    def test_fulltext_fetch_limits_roundtrip_to_source_budget(self):
        from simple_ar.research.sources.base import build_source_plan

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.toml"
            path.write_text(
                '[task]\ngoal="Compare source evidence"\n'
                '[research]\nuse_fulltext=true\nallow_pdf_download=true\nkeep_raw_pdf=true\n'
                'max_fulltext_documents=3\nmax_pdf_mb=16\n', encoding="utf-8",
            )
            argv = ["research-session", "--config", str(path)]
            defaults = research_defaults(argv)
            args = build_parser(research_defaults=defaults).parse_args(argv)
            self.assertEqual(args.research_max_fulltext_documents, 3)
            self.assertEqual(args.research_max_pdf_mb, 16)
            plan = build_source_plan(
                topic=args.topic, problem_markdown="",
                config={
                    "research_use_fulltext": args.research_use_fulltext,
                    "research_allow_pdf_download": args.research_allow_pdf_download,
                    "research_keep_raw_pdf": args.research_keep_raw_pdf,
                    "research_max_fulltext_documents": args.research_max_fulltext_documents,
                    "research_max_pdf_mb": args.research_max_pdf_mb,
                }, default_query=args.topic, default_max_results=args.max_results,
            )
            self.assertEqual(plan.budget["max_fulltext_documents"], 3)
            self.assertEqual(plan.budget["max_pdf_mb"], 16)
            self.assertTrue(plan.budget["keep_raw_pdf"])

    def test_config_can_record_an_explicit_idea_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.toml"
            path.write_text(
                '[task]\ngoal="Continual learning"\noutputs=["experiments"]\n'
                'selected_idea_id="idea-replay"\n',
                encoding="utf-8",
            )
            argv = ["research-session", "--config", str(path)]
            defaults = research_defaults(argv)
            self.assertEqual(defaults["selected_idea_id"], "idea-replay")
            args = build_parser(research_defaults=defaults).parse_args(argv)
            self.assertEqual(args.selected_idea_id, "idea-replay")

    def test_resume_applies_report_only_changes_and_rejects_unapplied_research_changes(self):
        from dataclasses import replace
        from simple_ar.cli.main import main
        from simple_ar.app.research_application import (
            ResearchApplication, ResearchApplicationError, ResearchApplicationServices, load_session,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = root / "paper.md"
            paper.write_text("# Local evidence\nA small supplied source for a bounded summary.\n", encoding="utf-8")
            config = root / "research.toml"

            def write_config(*, max_results=10, reviewer="llm", outputs=("summary",), model=""):
                config.write_text(
                    '[task]\ngoal="Summarize local evidence"\n'
                    f'outputs={json.dumps(list(outputs))}\noutput_root="out"\n'
                    f'[model]\nname="{model}"\n'
                    '[research]\nproviders=["local_files"]\nmaterials_only=true\n'
                    f'max_results={max_results}\n'
                    f'[assets]\npapers=[{json.dumps(str(paper))}]\n'
                    f'[report]\nreviewer="{reviewer}"\n',
                    encoding="utf-8",
                )

            write_config()
            session = root / 'out' / 'historical'
            self._historical_summary(session, [paper], goal='Summarize local evidence')
            before = load_session(session).view()

            def snapshot():
                return {str(path.relative_to(session)): path.read_bytes()
                        for path in session.rglob("*") if path.is_file()}

            # The same application entry owns CLI and programmatic rejection;
            # explicit empty/zero terms cannot disappear into omitted defaults.
            original_files = snapshot()
            for request, message in (
                ({"research_settings": {"max_results": 11}}, "max_results.*not applied"),
                ({"research_settings": {"research_materials_only": False}}, "research_materials_only.*not applied"),
                ({"report_writing_method": "article"}, "Saved reports keep their writing method"),
                ({"reanalyze": True, "additional_attempts": 0}, "cannot be combined"),
                ({"recover_interrupted": True, "authorize_remaining": {}}, "separate resume"),
                ({"recover_interrupted": True, "revised_report_config": {}}, "separate resume"),
                ({"reanalyze": True, "revised_brief": replace(load_session(session).brief, objective="Changed goal")}, "cannot be combined"),
                ({"decision_id": "0123456789abcdef", "decision_response": "reject"}, "not the current pending"),
            ):
                with self.subTest(request=request), self.assertRaisesRegex(ResearchApplicationError, message):
                    load_session(session).continue_session(**request)
                self.assertEqual(snapshot(), original_files)
            load_session(session).continue_session(resume_if_needed=True)
            self.assertEqual(snapshot(), original_files)

            write_config(max_results=11)
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(SystemExit, "max_results.*not applied"):
                main(["research-session", "--config", str(config), "--session-root", str(session)])
            unchanged = load_session(session).view()
            self.assertEqual(unchanged.state_refs, before.state_refs)
            self.assertEqual(unchanged.attempts, before.attempts)

            write_config(reviewer="disabled")
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(
                SystemExit, "Report settings do not request a report deliverable",
            ):
                main(["research-session", "--config", str(config), "--session-root", str(session)])
            no_report_change = load_session(session).view()
            self.assertEqual(no_report_change.state_refs, before.state_refs)
            self.assertEqual(no_report_change.attempts, before.attempts)

            report_app = load_session(session)
            # Pre-method sessions retain section/checkpoint behavior when the
            # caller omits today's new-task Writer default.
            report_app.services = replace(report_app.services, config={
                key: value for key, value in report_app.services.config.items()
                if key != "report_writing_method"
            })
            report_app.request_report()
            report_app.controller.pause("test checkpoint before report writing")
            before_report = load_session(session).view()

            write_config(
                reviewer="disabled", outputs=("summary", "report"), model="scripted",
            )
            with (
                patch.object(ResearchApplication, "continue_session", autospec=True,
                             side_effect=ResearchApplication.continue_session) as resume,
                patch.object(ResearchApplication, "advance", side_effect=RuntimeError("stop before report work")),
                patch("simple_ar.cli.main._optional_research_llm_client", return_value=object()),
                redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(SystemExit, "stop before report work"),
            ):
                main(["research-session", "--config", str(config), "--session-root", str(session)])
            forwarded = resume.call_args.kwargs
            self.assertTrue(forwarded["resume_if_needed"])
            self.assertEqual(forwarded["revised_report_config"], {"reviewer": "disabled"})
            for key in ("interaction", "report_writing_method", "authorize_remaining",
                        "additional_attempts", "additional_no_progress"):
                self.assertIsNone(forwarded[key], key)
            reply_services = ResearchApplicationServices(llm_client=object())
            refreshed = load_session(session, services=reply_services)
            self.assertEqual(refreshed.services.config["report"]["reviewer"], "disabled")
            self.assertEqual(refreshed.view().state_refs["summary"], before_report.state_refs["summary"])
            self.assertEqual(refreshed.view().attempts, before_report.attempts)

            # A delivery answer binds the complete explicit revision, including
            # false, zero and an empty mapping. Later replies retain that binding
            # even after another decision becomes current.
            refreshed._store_interaction_checkpoint(
                stage="delivery", action="report_write", question="Confirm delivery",
                reason="Review report configuration", options=({"id": "revise", "label": "Revise"},),
                evidence_refs=[], identity=refreshed._interaction_identity("delivery", "report_write"),
            )
            gate = refreshed.view().work_plan["interaction"]["decision"]
            terms = {"reviewer": "llm", "document_review": False, "max_section_tokens": 0, "figures": {}}
            reply = {"decision_id": gate["id"], "decision_response": "revise", "revised_report_config": terms}
            revised = refreshed.continue_session(**reply)
            self.assertEqual(revised.attempts, before_report.attempts)
            self.assertEqual(revised.state_refs["summary"], before_report.state_refs["summary"])
            for key, value in terms.items():
                self.assertEqual(refreshed.services.config["report"][key], value)
            self.assertNotIn("report_writing_method", refreshed.services.config)
            refreshed._store_interaction_checkpoint(
                stage="required_input", action="report_write", question="Specify audience",
                reason="Await audience", options=({"id": "revise", "label": "Clarify"},),
                evidence_refs=[], identity=refreshed._interaction_identity("required_input", "report_write"),
            )
            saved_files = snapshot()
            load_session(session, services=reply_services).continue_session(**reply)
            self.assertEqual(snapshot(), saved_files)
            with self.assertRaisesRegex(ResearchApplicationError, "different terms"):
                load_session(session, services=reply_services).continue_session(
                    **{**reply, "revised_report_config": {**terms, "max_section_tokens": 1}},
                )
            self.assertEqual(snapshot(), saved_files)

    def test_unknown_or_invalid_values_do_not_silently_use_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.toml"
            for source in ('[budget]\ntotal_token=100', '[budget]\ntotal_tokens=true',
                           '[task]\noutputs=["anything"]', '[model]\napi_key="secret"',
                           '[execution]\npairs=["not a pair"]', '[execution]\nprotocol="not a table"',
                           '[research]\nuse_fulltext="true"',
                           '[[execution.pairs]]\nseed=0\nbaseline_command=[]\ncandidate_command=["python"]'):
                with self.subTest(source=source):
                    path.write_text(source, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        research_defaults(["research-session", "--config", str(path)])
            for fields, message in (
                    ('seeds=[true]', 'execution.seeds'),
                    ('seeds=[4,9]\nseed_count=true', 'seed_count'),
                    ('seeds=[4,9]\nseed_count=0', 'seed_count'),
                    ('seed_count=2\nseed_flag=false', 'seed_flag'),
                    ('seed_flag=" "', 'seed_flag'),
                    ('baseline_policy=false', 'baseline_policy'),
                    ('baseline_policy=""', 'baseline_policy'),
                    ('baseline_policy=" "', 'baseline_policy'),
                    ('baseline="wrong"', 'baseline'),
                    ('baseline_ref=3', 'baseline_ref'),
                    ('baseline_ref=false', 'baseline_ref'),
                    ('baseline_ref={path="prior.json"}', 'baseline_ref'),
                    ('baseline_ref=" "', 'baseline_ref'),
                    ('baseline_ref=[]', 'baseline_ref'),
                    ('baseline_ref=[""]', 'baseline_ref'),
                    ('baseline_ref=["prior.json", " "]', 'baseline_ref'),
                    ('baseline_ref=["prior.json", 3]', 'baseline_ref'),
                    ('baseline_ref=["prior.json", false]', 'baseline_ref'),
                    ('baseline_ref=[["prior.json"]]', 'baseline_ref'),
                    ('baseline_ref=[{path="prior.json"}]', 'baseline_ref')):
                with self.subTest(fields=fields):
                    path.write_text('[execution]\n' + fields, encoding='utf-8')
                    with self.assertRaisesRegex(ValueError, message):
                        research_defaults(['research-session', '--config', str(path)])
            path.write_text('[execution]\nseed_count=2\nseed_flag="--seed"\nbaseline_policy="skip"', encoding='utf-8')
            argv = ['research-session', '--config', str(path)]
            with patch('simple_ar.app.research_execution.normalize_execution_config',
                       side_effect=AssertionError('Config-only declarations must not normalize argv')):
                defaults = research_defaults(argv)
            self.assertNotIn('command_argv', defaults)
            self.assertEqual(defaults['execution_details'],
                {'seed_count': 2, 'seed_flag': '--seed', 'baseline_policy': 'skip'})
            args = build_parser(research_defaults=defaults).parse_args([
                *argv, '--topic', 'Explicit command', '--command', sys.executable])
            self.assertEqual(args.command_argv, [sys.executable])

    def test_external_command_config_is_not_a_research_config(self):
        self.assertEqual(research_defaults(["research-session", "--topic", "test", "--command",
                                           "python", "train.py", "--config", "missing.toml"]), {})

    def test_templates_parse_with_same_defaults(self):
        from simple_ar.app.research_application import ResearchApplicationServices

        defaults = build_parser().parse_args(["research-session", "--topic", "A research goal"])
        self.assertIsNone(defaults.total_tokens)
        self.assertIsNone(defaults.llm_requests)
        self.assertEqual(ResearchApplicationServices().budget_limits,
                         {"total_tokens": None, "llm_requests": None})
        root = Path(__file__).resolve().parents[1]
        for name, path in (("survey", "examples/survey/research.toml"),
                           ("advanced", "tests/fixtures/research_config/advanced.toml"),
                           ("continual", "examples/continual_learning/research.toml")):
            argv = ["research-session", "--config", str(root / path)]
            args = build_parser(research_defaults=research_defaults(argv)).parse_args(argv)
            self.assertTrue(args.topic)
            self.assertEqual(args.max_output_tokens, 8192)
            self.assertIsNone(args.total_tokens)
            self.assertIsNone(args.llm_requests)
            if name == "advanced":
                self.assertTrue(args.research_use_fulltext)
                self.assertTrue(args.research_allow_pdf_download)
                self.assertTrue(args.research_keep_raw_pdf)

    def test_tabm_example_declares_extensible_single_seed_protocol(self):
        import tomllib
        from simple_ar.app.research_execution import normalize_execution_config

        root = Path(__file__).resolve().parents[1]
        with (root / "examples/tabm_research/research.toml").open("rb") as stream:
            example = tomllib.load(stream)
        boundary = example["execution"]
        self.assertEqual(boundary["seeds"], [0])
        self.assertEqual(boundary["seed_flag"], "--seed")
        defaults = research_defaults([
            "research-session", "--config", str(root / "examples/tabm_research/research.toml"),
        ])
        self.assertEqual(defaults["execution_details"]["seeds"], [0])
        self.assertEqual(defaults["execution_details"]["seed_flag"], "--seed")
        command = ["python", "run_tabm.py", "--seed", "0"]
        accepted = normalize_execution_config({
            "command": command,
            "baseline": {"command": command},
            "timeout_sec": boundary["timeout_sec"],
            "seeds": boundary["seeds"],
            "seed_flag": boundary["seed_flag"],
        })
        self.assertEqual(len(accepted["pairs"]), 1)
        self.assertEqual(accepted["pairs"][0]["seed"], 0)
        self.assertEqual(accepted["protocol_seed_flag"], "--seed")

    def test_report_figures_are_forwarded_as_existing_report_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research.toml"
            path.write_text(
                '[task]\ngoal="Measured report"\noutputs=["experiments", "report"]\n'
                '[report]\nmax_section_tokens=0\n[report.figures]\nenabled=true\nmax_figures=2\n',
                encoding="utf-8",
            )
            defaults = research_defaults(["research-session", "--config", str(path)])
            self.assertEqual(defaults["report_figures"], {"enabled": True, "max_figures": 2})
            self.assertEqual(defaults["max_section_tokens"], 0)
