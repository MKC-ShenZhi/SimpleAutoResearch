import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from simple_ar.app.research_application import ResearchApplicationServices, create_session, load_session
from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext
from simple_ar.integrations.llm import LLMClient, LLMError, LLMSettings
from simple_ar.research.workflow_contracts import ResearchBrief
from simple_ar.report.capability import ReportAssemblyRequest, run_report_capability
from simple_ar.report.schema import AgentReportResult, ReportSectionDraft, ReportRuntimeConfig
from simple_ar.result_analysis.table import load_analysis_package, rebuild


class AnalysisWritingTests(unittest.TestCase):
    def test_data_report_accepts_documentation_and_references_through_ordinary_config(self):
        from simple_ar.cli.parser import build_parser
        from simple_ar.cli.start import prepare_start
        from simple_ar.cli.research_config import research_defaults, validate_session_arguments
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, notes, paper = root / 'values.csv', root / 'dictionary.md', root / 'reference.txt'
            source.write_text('score\n1\n3\n', encoding='utf-8')
            notes.write_text('Each row is one laboratory observation; score is recorded in points.', encoding='utf-8')
            paper.write_text('A supplied reference, not measurements from this session.', encoding='utf-8')
            common = ['start', '--kind', 'data_analysis', '--goal', 'Describe supplied observations',
                '--data-file', str(source), '--value-column', 'score', '--observation-unit', 'laboratory observation',
                '--material', str(notes), '--document', str(paper), '--output-root', str(root / 'runs'), '--prepare-only']
            parser = build_parser()
            with patch('sys.stdin.isatty', return_value=False), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, 'requires writing|require --with-report'):
                    prepare_start(parser.parse_args(common))
                config = prepare_start(parser.parse_args([*common, '--with-report']))
            defaults = research_defaults(['research-session', '--config', str(config)])
            self.assertEqual(defaults['material'], [str(notes.resolve())])
            self.assertEqual(defaults['local_document'], [str(paper.resolve())])
            self.assertTrue(defaults['research_materials_only'])
            self.assertFalse(defaults['research_allow_pdf_download'])
            advanced = parser.parse_args(['research-session', '--topic', 'Describe supplied values', '--task-kind', 'data_analysis',
                '--data-file', str(source), '--value-column', 'score', '--observation-unit', 'row',
                '--material', str(notes), '--with-report'])
            self.assertEqual(validate_session_arguments(advanced).materials, [str(notes)])
            for overrides, error in (([], None), (['--outputs', 'data_analysis'], 'documentation'),
                    (['--material', str(source)], 'Report inputs must be local'), (['--provider', 'arxiv'], 'without research')):
                with self.subTest(overrides=overrides):
                    args = build_parser(research_defaults=defaults).parse_args(
                        ['research-session', '--config', str(config), *overrides])
                    if error:
                        with self.assertRaisesRegex(SystemExit, error):
                            validate_session_arguments(args)
                    else:
                        self.assertEqual(validate_session_arguments(args).outputs, ['data_analysis', 'report'])

    def test_documentation_is_retained_as_material_not_measurement_after_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, notes = root / 'values.csv', root / 'dictionary.md'
            source.write_text('score\n1\n3\n', encoding='utf-8')
            notes.write_text('# Variables\nOne row is a recorded specimen. Values are already scaled; do not rescale.\n', encoding='utf-8')
            app = create_session(ResearchBrief(request_text='Explain the supplied values using their dictionary',
                requested_outputs=('data_analysis', 'report'), asset_requests=({
                    'locator': str(notes), 'role': 'material', 'mutability': 'read_only',
                    'allowed_uses': ['read', 'reference']},)), root=root / 'session',
                services=ResearchApplicationServices(config={'research_task_kind': 'data_analysis',
                    'data_analysis': {'file': str(source), 'value_columns': ['score'], 'observation_unit': 'specimen'}},
                    budget_limits={'process_invocations': 0}))
            view = app.advance(max_actions=4)
            self.assertEqual(view.next_action, 'report_write', view.status_reason)
            documents_ref = view.state_refs['documents']
            documents = app.controller.store.resolve(documents_ref).read_bytes()
            notes.unlink()
            source.unlink()
            restored = load_session(root / 'session', services=ResearchApplicationServices(
                llm_client=LLMClient(LLMSettings(api_key='fixture'))))

            def writer(**kwargs):
                handles = kwargs['context'].source_handles
                data = [row for row in handles if row.metadata['evidence_role'] == 'recomputed_from_user_supplied_data']
                material = [row for row in handles if row.metadata['evidence_role'] == 'user_supplied_unverified']
                self.assertEqual(len(data), 1)
                self.assertEqual(len(material), 1)
                self.assertEqual(material[0].kind, 'material')
                self.assertIn('already scaled', json.dumps(material[0].metadata['evidence_passages']))
                self.assertEqual(kwargs['context'].results['supplied_analyses'][0]['records'][0]['mean'], 2)
                return AgentReportResult(report_body='', memory=kwargs['memory'], used_agent=True,
                    sections=[ReportSectionDraft(section_id='findings', heading='Findings',
                        draft_markdown='The recorded mean is 2. The supplied dictionary describes scaled values.')])

            with patch('simple_ar.report.writing.run_report_agent', side_effect=writer):
                view = restored.advance(max_actions=3)
            self.assertEqual(view.status, 'completed', view.status_reason)
            self.assertEqual(restored.controller.store.resolve(documents_ref).read_bytes(), documents)
            self.assertFalse({'search', 'read', 'synthesis', 'experiment'} & view.state_refs.keys())

    def test_guided_analysis_can_request_report_in_its_own_configuration(self):
        from simple_ar.cli.parser import build_parser
        from simple_ar.cli.start import prepare_start
        from simple_ar.cli.research_config import research_defaults, validate_session_arguments
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.csv'
            source.write_text('label,score\nA,1\nA,3\nB,-2\n', encoding='utf-8')
            common = ['start', '--kind', 'data_analysis', '--goal', 'Analyze and explain these supplied scores',
                      '--data-file', str(source), '--group-column', 'label', '--value-column', 'score',
                      '--observation-unit', 'one supplied row', '--value-unit', 'points', '--prepare-only']
            parser = build_parser()
            with patch('sys.stdin.isatty', return_value=False), \
                    patch('simple_ar.cli.start.select_start_model'), contextlib.redirect_stdout(io.StringIO()):
                plain = prepare_start(parser.parse_args([*common, '--output-root', str(root / 'plain')]))
                report = prepare_start(parser.parse_args([*common, '--with-report', '--model', 'env',
                    '--output-root', str(root / 'report')]))
            plain_defaults = research_defaults(['research-session', '--config', str(plain)])
            defaults = research_defaults(['research-session', '--config', str(report)])
            self.assertEqual(plain_defaults['outputs'], ['data_analysis'])
            self.assertEqual(plain_defaults['model'], '')
            self.assertEqual(defaults['outputs'], ['data_analysis', 'report'])
            self.assertEqual(defaults['model'], 'env')
            self.assertEqual(defaults['report_template'], 'material_report')
            self.assertEqual(defaults['report_draft_scope'], 'document')
            self.assertEqual(defaults['report_review_scope'], 'document')
            self.assertNotIn('[report]', plain.read_text())
            legacy = root / 'legacy.toml'
            legacy.write_text(report.read_text().replace('draft_scope = "document"\n', '').replace('review_scope = "document"\n', ''))
            old = build_parser(research_defaults=research_defaults(['research-session', '--config', str(legacy)])).parse_args(
                ['research-session', '--config', str(legacy)])
            from simple_ar.cli.research_config import report_settings
            old_config = ReportRuntimeConfig.model_validate(report_settings(old))
            self.assertEqual(old_config.draft_scope, 'section')
            self.assertEqual(old_config.review_scope, 'section')
            parser = build_parser(research_defaults=defaults)
            args = parser.parse_args(['research-session', '--config', str(report)])
            self.assertEqual(validate_session_arguments(args).outputs, ['data_analysis', 'report'])
            self.assertNotIn('command_argv', defaults)
            from simple_ar.cli.main import main
            from simple_ar.cli.research_config import report_writing_method
            from simple_ar.tools.writing import freeze_report_requirement
            method, settings = report_writing_method(args, report_settings(args))
            settings['mode'] = 'research_only'
            analysis = validate_session_arguments(args).data_analysis
            # The CLI owns contract projection and routing. Writer/projection,
            # recovery and delivery behavior have their own domain tests below.
            with patch('simple_ar.cli.main._optional_research_llm_client', return_value=object()), \
                    patch('simple_ar.app.tool_application.ToolApplication.create') as create_native, \
                    patch('simple_ar.app.research_application.create_session') as create_legacy, \
                    patch('simple_ar.cli.main._render_native_result'), \
                    contextlib.redirect_stdout(io.StringIO()):
                create_native.return_value.run.return_value = {'status': 'completed'}
                main(['research-session', '--config', str(report), '--interaction', 'autonomous'])
            create_native.assert_called_once()
            create_legacy.assert_not_called()
            options = create_native.call_args.kwargs
            self.assertEqual(options['report_requirement'], freeze_report_requirement(settings, method=method))
            self.assertEqual(options['table_analysis'], {'path': 'source0',
                'spec': {key: value for key, value in analysis.items() if key != 'file'}})
            self.assertEqual(options['sources'], {'source0': source.resolve()})
            self.assertEqual(options['model'], 'env')
            self.assertFalse(options['allow_execution'])
            self.assertFalse(options['allow_network'])
            self.assertEqual(options['research_policy']['materials'], [])
            self.assertEqual(options['process_limits'], {'process_invocations': 0, 'process_wall_seconds': 0})
            create_native.return_value.run.assert_called_once()
            self.assertNotIn('actions', create_native.return_value.run.call_args.kwargs)

    def test_analysis_report_uses_same_session_and_restores_frozen_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.csv'
            source.write_text('label,score\nA,1\nA,3\nB,-2\n', encoding='utf-8')
            app = create_session(ResearchBrief(request_text='Analyze and write a results note',
                requested_outputs=('data_analysis', 'report')), root=root / 'session',
                services=ResearchApplicationServices(config={'research_task_kind': 'data_analysis',
                    'data_analysis': {'file': str(source), 'value_columns': ['score'],
                        'observation_unit': 'one supplied row', 'value_unit': 'points', 'group_column': 'label'}},
                    budget_limits={'process_invocations': 0}))
            view = app.advance(max_actions=4)
            self.assertEqual(view.next_action, 'report_write', view.status_reason)
            analysis_ref = view.state_refs['data_analysis']
            frozen = app.controller.store.resolve(analysis_ref).read_bytes()
            source.unlink()
            app = load_session(root / 'session', services=ResearchApplicationServices(
                llm_client=LLMClient(LLMSettings(api_key='fixture'))))

            def writer(**kwargs):
                context, memory = kwargs['context'], kwargs['memory']
                self.assertEqual(context.report_mode, 'supplied_materials')
                self.assertEqual(context.results['session_execution'], 'not_requested')
                self.assertEqual(context.results['supplied_analyses'][0]['records'][0]['mean'], 2)
                self.assertEqual(context.source_handles[0].metadata['evidence_role'],
                                 'recomputed_from_user_supplied_data')
                return AgentReportResult(report_body='', memory=memory, used_agent=True,
                    sections=[ReportSectionDraft(section_id='discussion', heading='Discussion',
                        draft_markdown='These supplied descriptive values do not establish causality or significance.')])

            with patch('simple_ar.report.writing.run_report_agent', side_effect=writer):
                view = app.advance(max_actions=3)
            self.assertEqual(view.status, 'completed', view.status_reason)
            self.assertEqual(app.controller.store.resolve(analysis_ref).read_bytes(), frozen)
            self.assertFalse({'experiment', 'read', 'synthesis', 'search'} & view.state_refs.keys())
            self.assertEqual(len([a for a in app.controller.list_attempts() if a.capability == 'data_analysis']), 1)
            output = app.controller.store.resolve(view.state_refs['report']).parent
            moved = root / 'portable report'
            shutil.copytree(output, moved)
            shutil.rmtree(root / 'session')
            package = moved / 'analyses/analysis-001/analysis.json'
            rebuild(package)
            self.assertEqual(load_analysis_package(package)[0]['records'][0]['mean'], 2)

    def test_data_report_plan_has_no_research_prerequisites_and_rejects_execution(self):
        from simple_ar.research.task_plan import TaskPlanRequest, build_task_plan
        settings = {'file': 'supplied.csv', 'value_columns': ['value'], 'observation_unit': 'row'}
        common = dict(task_kind='data_analysis', goal='Explain supplied values', request_text='Explain supplied values',
                      config={'data_analysis': settings})
        plan = build_task_plan(TaskPlanRequest(**common, requested_outputs=('data_analysis', 'report')))
        self.assertEqual([s.action for s in plan.steps],
            ['data_ingest', 'data_analysis', 'document_ingest', 'report_write', 'report', 'report_audit'])
        for outputs, execution in ((('report',), None), (('data_analysis', 'experiments'), None),
                                   (('data_analysis', 'report'), {'argv': ['python', 'train.py']})):
            with self.subTest(outputs=outputs, execution=execution), self.assertRaises(ValueError):
                build_task_plan(TaskPlanRequest(**common, requested_outputs=outputs, execution=execution))
        with self.assertRaisesRegex(ValueError, 'documentation'):
            build_task_plan(TaskPlanRequest(**{**common, 'config': {**common['config'],
                'research_local_documents': ['dictionary.md']}}, requested_outputs=('data_analysis',)))

    def package(self, root, mode='observations'):
        source = root / 'source.csv'
        source.write_text('label,score\nA,1\nA,3\nB,-2\n' if mode == 'observations' else 'label,score\nA,2\nB,-2\n', encoding='utf-8')
        app = create_session(ResearchBrief(request_text='Describe', requested_outputs=('data_analysis',)),
            root=root / 'data-session', services=ResearchApplicationServices(config={
                'research_task_kind': 'data_analysis', 'data_analysis': {'file': str(source),
                    'value_columns': ['score'], 'observation_unit': 'one supplied row', 'value_unit': 'points',
                    'group_column': 'label', 'mode': mode}}))
        view = app.advance(max_actions=10)
        self.assertEqual(view.status, 'completed', view.status_reason)
        return app.controller.store.resolve(view.state_refs['data_analysis'])

    def package_input(self, root):
        path = self.package(root)
        store = ArtifactStore(path.parent)
        return path, store, store.ref(path.name, kind='table_analysis', schema='table_analysis.v1')

    def test_rechecks_package_rejects_stale_records_and_raw_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.package(root)
            result, _, _ = load_analysis_package(path)
            self.assertEqual(result['records'][0]['mean'], 2)
            self.assertIn('value unit (user-declared): points',
                          json.loads(path.read_text(encoding='utf-8'))['figures'][0]['caption'])
            original = path.read_text(encoding='utf-8')
            stale = json.loads(original)
            stale['records'][0]['mean'] = 999
            missing_source = json.loads(original)
            missing_source['source'] = {}
            for contents, error in ((json.dumps(stale), 'no longer matches'),
                    ('[{"score":1}]', 'table_analysis.v1'),
                    ('{"schema_version":"table_analysis.v1","status":"completed"}', 'column settings'),
                    (json.dumps(missing_source), 'relative path')):
                with self.subTest(error=error):
                    path.write_text(contents, encoding='utf-8')
                    with self.assertRaisesRegex(ValueError, error):
                        load_analysis_package(path)

    def test_pre_coordinate_v1_package_keeps_bar_defaults_and_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.package(Path(directory))
            payload = json.loads(path.read_text(encoding='utf-8'))
            for field in ('plot', 'x_column', 'x_unit', 'max_points'):
                payload['spec'].pop(field, None)
            path.write_text(json.dumps(payload), encoding='utf-8')
            result, _, _ = load_analysis_package(path)
            self.assertEqual(result['spec']['plot'], 'bar')
            self.assertEqual(result['records'], payload['records'])
            rebuild(path)

    def test_rejects_package_input_escape_and_missing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.package(root)
            payload = json.loads(path.read_text(encoding='utf-8'))
            for target in ('../../../source.csv', str(root / 'source.csv')):
                with self.subTest(target=target):
                    payload['source']['path'] = target
                    path.write_text(json.dumps(payload), encoding='utf-8')
                    with self.assertRaisesRegex(ValueError, 'inside|relative'):
                        load_analysis_package(path)
            payload['source']['path'] = 'missing.csv'
            path.write_text(json.dumps(payload), encoding='utf-8')
            with self.assertRaises(FileNotFoundError):
                load_analysis_package(path)

    def test_guided_writing_uses_existing_material_config_and_checks_before_saving(self):
        from simple_ar.cli.parser import build_parser
        from simple_ar.cli.start import prepare_start
        from simple_ar.cli.research_config import research_defaults
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.package(root)
            argv = ['start', '--kind', 'writing', '--goal', 'Explain data honestly', '--material', str(path),
                    '--template', 'material_report', '--output-root', str(root / 'guided'), '--prepare-only']
            with patch('sys.stdin.isatty', return_value=False), contextlib.redirect_stdout(io.StringIO()):
                config = prepare_start(build_parser().parse_args(argv))
            settings = research_defaults(['research-session', '--config', str(config)])
            self.assertEqual(settings['material'], [str(path)])
            self.assertNotIn('command_argv', settings)
            path.write_text('{"schema_version":"table_analysis.v1"}', encoding='utf-8')
            argv[argv.index('--output-root')+1] = str(root / 'invalid')
            with patch('sys.stdin.isatty', return_value=False), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                prepare_start(build_parser().parse_args(argv))
            self.assertFalse((root / 'invalid').exists())

    def test_writing_freezes_rechecked_data_and_delivers_movable_figures_without_experiments(self):
        from simple_ar.report.agent import _compact_execution_results, _prompt_handle_view
        for mode in ('observations', 'values'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = self.package(root, mode)
                app = create_session(ResearchBrief(request_text='Explain supplied data', requested_outputs=('report',),
                    asset_requests=({'locator': str(path), 'role': 'material'},)), root=root / 'writing-session',
                    services=ResearchApplicationServices(config={'research_task_kind': 'writing'}, budget_limits={'process_invocations': 0}))
                view = app.advance(max_actions=2)
                self.assertEqual(view.next_action, 'report_write', view.status_reason)
                (root / 'data-session').rename(root / 'hidden-original-data-session')
                app = load_session(root / 'writing-session', services=ResearchApplicationServices(
                    llm_client=LLMClient(LLMSettings(api_key='fixture'))))
                with patch('simple_ar.report.writing.run_report_agent', side_effect=LLMError('Temporary outage')):
                    self.assertEqual(app.advance(max_actions=1).status, 'paused')
                app = load_session(root / 'writing-session', services=ResearchApplicationServices(
                    llm_client=LLMClient(LLMSettings(api_key='fixture'))))
                app.continue_session()

                def writer(**kwargs):
                    context, memory = kwargs['context'], kwargs['memory']
                    from simple_ar.report.document_plan import supplied_figure_sources
                    figures = supplied_figure_sources(context)
                    self.assertEqual(len(figures), 1)
                    self.assertEqual(figures[0]['figure_count'], 1)
                    self.assertNotIn('captions', figures[0])
                    handle = context.source_handles[0]
                    self.assertEqual(figures[0]['handle'], handle.handle)
                    self.assertFalse(any('![Descriptive values]' in passage['text']
                        for passage in handle.metadata['evidence_passages']))
                    self.assertEqual(_prompt_handle_view(handle)['metadata']['evidence_role'], 'recomputed_from_user_supplied_data')
                    compact = _compact_execution_results(context.results)['supplied_analyses'][0]
                    self.assertTrue(compact['figures'][0]['caption'])
                    self.assertEqual(figures[0]['document_id'], compact['document_id'])
                    self.assertEqual(compact['records'][0]['mean' if mode == 'observations' else 'value'], 2)
                    self.assertEqual(compact['input_row_count'], 3 if mode == 'observations' else 2)
                    self.assertIn('not individual raw rows' if mode == 'observations' else 'Supplied values/coordinates', compact['records_scope'])
                    self.assertEqual(context.metric_sources, [])
                    self.assertEqual(context.results['session_execution'], 'not_requested')
                    return AgentReportResult(report_body='', memory=memory, used_agent=True,
                        sections=[ReportSectionDraft(section_id='discussion', heading='Discussion',
                            draft_markdown='These are descriptive values from supplied data. No experiment, significance test or causal analysis was performed.')])

                with patch('simple_ar.report.writing.run_report_agent', side_effect=writer):
                    view = app.advance(max_actions=3)
                self.assertEqual(view.status, 'completed', view.status_reason)
                self.assertNotIn('experiment', view.state_refs)
                self.assertNotIn('synthesis', view.state_refs)
                output = app.controller.store.resolve(view.state_refs['report']).parent
                body = (output / 'report_body.md').read_text(encoding='utf-8')
                self.assertIn('Supplied Descriptive Data', body)
                self.assertNotIn('Verified Experiment Metrics', body)
                self.assertNotIn('| A | score |', body)
                self.assertIn('[numerical records](analyses/analysis-001/analysis.json)', body)
                self.assertTrue((output / 'analyses/analysis-001/figures/value-1-1.svg').is_file())
                moved = root / 'moved report with spaces'
                shutil.copytree(output, moved)
                (root / 'writing-session').rename(root / 'hidden-writing-session')
                rebuild(moved / 'analyses/analysis-001/analysis.json')
                self.assertEqual(load_analysis_package(moved / 'analyses/analysis-001/analysis.json')[0]['records'][0]
                                 ['mean' if mode == 'observations' else 'value'], 2)
                app = load_session(root / 'hidden-writing-session')
                before = len(app.controller.list_attempts())
                self.assertEqual(app.advance(max_actions=10).status, 'completed')
                self.assertEqual(len(app.controller.list_attempts()), before)

    def test_unknown_json_pauses_before_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'raw.json'
            path.write_text('[{"score": 1}]', encoding='utf-8')
            app = create_session(ResearchBrief(request_text='Write', requested_outputs=('report',),
                asset_requests=({'locator': str(path), 'role': 'material'},)), root=root / 'session',
                services=ResearchApplicationServices(config={'research_task_kind': 'writing'}))
            view = app.advance(max_actions=10)
            self.assertEqual(view.status, 'paused')
            self.assertNotIn('writer', view.state_refs)

    def test_assembly_places_data_only_with_unique_frozen_source_owner_and_preserves_full_records(self):
        from simple_ar.report.schema import ReportDocumentPlan, ReportSectionPlan
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, source, ref = self.package_input(root)
            original = path.read_bytes()
            drafts = (ReportSectionDraft(section_id='findings', heading='Findings', draft_markdown='Supported observations.'),
                      ReportSectionDraft(section_id='limits', heading='Limits', draft_markdown='No population inference.'))
            for owner_count in (0, 1, 2):
                for detail in ('linked', 'full'):
                    with self.subTest(owner_count=owner_count, detail=detail):
                        store = ArtifactStore(root / f'report-{owner_count}-{detail}')
                        plan = ReportDocumentPlan(sections=[ReportSectionPlan(
                            section_id=row.section_id, heading=row.heading, goal='Explain supplied evidence',
                            evidence_handles=['material:data'] if index < owner_count else [])
                            for index, row in enumerate(drafts)])
                        request = ReportAssemblyRequest(title='Describe', sections=drafts, table_analyses=(ref,),
                            analysis_handles={ref.path: 'material:data'}, document_plan=plan,
                            config={'data_tables': detail})
                        context = CapabilityContext(store=store, input_store=source, inputs=(ref,),
                            attempt=AttemptManifest(attempt_id='report-1', capability='report'))
                        self.assertEqual(run_report_capability(context=context, request=request).status, 'completed')
                        body = store.read_text('report_body.md')
                        self.assertEqual('| A | score |' in body, detail == 'full')
                        self.assertEqual('Supplied Descriptive Data' in body, owner_count != 1)
                        if owner_count == 1:
                            self.assertLess(body.index('analyses/analysis-001/analysis.md'), body.index('## Limits'))
                        records = load_analysis_package(store.root / 'analyses/analysis-001/analysis.json')[0]['records']
                        # The same pure block is visible before writing/review,
                        # and assembly must not invent additional reader prose.
                        from simple_ar.report.data_delivery import analysis_delivery_block
                        preview = analysis_delivery_block(json.loads(path.read_text()), index=1,
                            handle='material:data', config=ReportRuntimeConfig(data_tables=detail),
                            plan=plan, section_ids=[row.section_id for row in drafts])
                        self.assertIn(preview['markdown'], body)
                        self.assertEqual(records, load_analysis_package(path)[0]['records'])
                        self.assertEqual(path.read_bytes(), original)
                        self.assertEqual(drafts[0].draft_markdown, 'Supported observations.')
                        # Source citations alone no longer select inline figures.
                        # The complete copied package remains editable/rebuildable.
                        self.assertFalse(store.exists('figures/figures_manifest.json'))
                        self.assertTrue(any((store.root / 'analyses/analysis-001/figures').glob('*.svg')))

    def test_explicit_data_visual_owner_overrides_shared_citations_without_duplicate_rendering(self):
        from simple_ar.report.schema import ReportDocumentPlan, ReportSectionPlan, ReportVisualIntent
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, inputs, ref = self.package_input(root)
            drafts = tuple(ReportSectionDraft(section_id=key, heading=key, draft_markdown='Supported observations.')
                           for key in ('scope', 'findings', 'limits'))
            plan = ReportDocumentPlan(sections=[ReportSectionPlan(section_id=row.section_id, heading=row.heading,
                goal='Explain', evidence_handles=['material:data']) for row in drafts],
                visual_intents=[ReportVisualIntent(visual_id='figure-1', kind='figure', title='Supplied values',
                    purpose='Compare measurements', section_id='findings', view='supplied-data',
                    evidence_handles=['material:data'])])
            store = ArtifactStore(root / 'assembled')
            context = CapabilityContext(store=store, input_store=inputs, inputs=(ref,),
                attempt=AttemptManifest(attempt_id='report-1', capability='report'))
            result = run_report_capability(context=context, request=ReportAssemblyRequest(title='Bounded data',
                sections=drafts, document_plan=plan, table_analyses=(ref,),
                analysis_handles={ref.path: 'material:data'}))
            self.assertEqual(result.status, 'completed')
            body = store.read_text('report_body.md')
            self.assertNotIn('Supplied Descriptive Data', body)
            self.assertLess(body.index('## findings'), body.index('analyses/analysis-001/analysis.md'))
            self.assertLess(body.index('analyses/analysis-001/analysis.md'), body.index('## limits'))
            figures = store.read_json('figures/figures_manifest.json')['figures']
            self.assertEqual(len(figures), 1)
            self.assertEqual(figures[0]['anchor'], 'findings')

    def test_assembly_respects_disabled_and_off_without_losing_data(self):
        from simple_ar.report.capability import assemble_report_document
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, inputs, ref = self.package_input(root)
            for settings, included in (({'enabled': True}, True), ({'enabled': False}, False), ({'mode': 'off'}, False)):
                store = ArtifactStore(root / str(settings))
                request = ReportAssemblyRequest(title='Describe', sections=(ReportSectionDraft(section_id='discussion',
                    heading='Discussion', draft_markdown='Supplied data only.'),), table_analyses=(ref,),
                    config={'figures': settings})
                context = CapabilityContext(store=store, input_store=inputs, inputs=(ref,),
                                            attempt=AttemptManifest(attempt_id='report-1', capability='report'))
                result = run_report_capability(context=context, request=request)
                body = store.read_text('report_body.md')
                self.assertEqual('![Descriptive data]' in body, included)
                self.assertTrue(store.exists('analyses/analysis-001/analysis.json'))
                self.assertEqual(len(result.artifacts), len({item.path for item in result.artifacts}))
                with self.assertRaisesRegex(ValueError, 'registered inputs'):
                    assemble_report_document(request, report_dir=store.root)

    def test_multiple_packages_keep_identity_and_explicit_figure_limit(self):
        from simple_ar.research.documents.ingest import DocumentIngestRequest, run_document_ingest_capability
        from simple_ar.research.contracts import SourcePlan
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for index in (1, 2):
                folder = root / str(index)
                folder.mkdir()
                paths.append(self.package(folder))
            raw_paths = []
            for name, payload in (('runtime', {'schema_version': 'producer.v1', 'seeds': [0, 1], 'device': 'cpu'}),
                                  ('observations', [{'repeat': 0, 'method': 'A', 'score': 0.25}])):
                path = root / f'{name}.json'
                path.write_text(json.dumps(payload), encoding='utf-8')
                raw_paths.append(path)
            store = ArtifactStore(root / 'ingest')
            result = run_document_ingest_capability(context=CapabilityContext(store=store,
                attempt=AttemptManifest(attempt_id='ingest-1', capability='document_ingest')), request=DocumentIngestRequest(
                papers=(), source_plan=SourcePlan(queries=[], local_documents=[str(path) for path in [*paths, *raw_paths]]),
                extraction_dir=root / 'extraction', analysis_paths=tuple([*paths, *raw_paths])))
            from simple_ar.research.documents.ingest import DocumentBundle
            bundle = DocumentBundle.from_handoff_dict(store.read_json(result.artifacts[0]))
            self.assertEqual(len(bundle.records), 4)
            self.assertEqual(len({row.document_id for row in bundle.records}), 4)
            raw_records = [row for row in bundle.records if row.source_id in {str(path) for path in raw_paths}]
            self.assertEqual(len(raw_records), 2)
            self.assertTrue(all(row.extraction_status == 'parsed' and 'table_analysis' not in row.metadata for row in raw_records))
            for record in raw_records:
                text = ''.join(row.text for row in bundle.sections if row.document_id == record.document_id)
                self.assertIn(Path(record.source_id).read_text(), text)
            from simple_ar.report.projection import build_material_report_inputs
            from simple_ar.report.document_plan import supplied_figure_sources
            material_context, material_memory = build_material_report_inputs(topic='Two supplied datasets',
                documents=bundle, documents_ref=result.artifacts[0], assets=())
            self.assertEqual(material_context.papers, [])
            raw_ids = {row.document_id for row in raw_records}
            self.assertTrue(all(handle.kind == 'material' and handle.metadata['evidence_role'] == 'user_supplied_unverified'
                for handle in material_context.source_handles if handle.metadata.get('document_id') in raw_ids))
            sources = supplied_figure_sources(material_context)
            self.assertEqual(len(sources), 2)
            self.assertEqual(len({row['handle'] for row in sources}), 2)
            self.assertTrue(all(row['figure_count'] == 1 for row in sources))
            self.assertTrue(all(row['row_count'] == 3 for row in material_context.results['supplied_analyses']))
            self.assertIn('2 supplied analysis package(s) retain results', material_context.evidence_summary)
            self.assertNotIn('No experiment', material_context.evidence_summary)
            self.assertTrue(any('No experiment' in text for text in material_memory.limitations))
            self.assertTrue(any('Neither certifies data collection' in text for text in material_memory.limitations))
            self.assertFalse(any('![Descriptive values]' in row.text for row in bundle.sections))
            refs = tuple(store.ref(row.metadata['table_analysis']['artifact']) for row in bundle.records
                         if 'table_analysis' in row.metadata)
            output = ArtifactStore(root / 'report')
            request = ReportAssemblyRequest(title='Two datasets', sections=(ReportSectionDraft(section_id='discussion',
                heading='Discussion', draft_markdown='Separate supplied datasets.'),), table_analyses=refs,
                config={'figures': {'max_figures': 1}})
            with self.assertRaisesRegex(ValueError, 'max_figures'):
                run_report_capability(context=CapabilityContext(store=output, input_store=store, inputs=refs,
                    attempt=AttemptManifest(attempt_id='report-1', capability='report')), request=request)
            self.assertFalse(output.exists('report.md'))
            # Exercise the real import/projection/plan/assembly chain, with
            # identical producer-local filenames in two distinct packages.
            from simple_ar.report.document_plan import resolve_document_plan
            from simple_ar.report.schema import ReportRuntimeConfig, ReportSectionPlan
            drafts = tuple(ReportSectionDraft(section_id=f'data-{index}', heading=f'Dataset {index}',
                draft_markdown='Only the registered supplied observations are described.')
                for index in (1, 2))
            plan = resolve_document_plan(sections=[ReportSectionPlan(section_id=draft.section_id,
                heading=draft.heading, goal='Explain this dataset', evidence_handles=[source['handle']])
                for draft, source in zip(drafts, sources)], contract=None, config=ReportRuntimeConfig(),
                supplied_figure_handles=[row['handle'] for row in sources],
                visual_candidates=[{'kind': 'figure', 'view': 'supplied-data', 'section_id': draft.section_id,
                    'title': draft.heading, 'purpose': 'Show the retained data', 'evidence_handles': [source['handle']]}
                    for draft, source in zip(drafts, sources)])
            delivered = ArtifactStore(root / 'two-dataset-report')
            finished = run_report_capability(context=CapabilityContext(store=delivered, input_store=store, inputs=refs,
                attempt=AttemptManifest(attempt_id='report-2', capability='report')),
                request=ReportAssemblyRequest(title='Two registered datasets', sections=drafts, document_plan=plan,
                    table_analyses=refs, analysis_handles={ref.path: source['handle'] for ref, source in zip(refs, sources)}))
            self.assertEqual(finished.status, 'completed')
            figure_rows = delivered.read_json('figures/figures_manifest.json')['figures']
            self.assertEqual([row['anchor'] for row in figure_rows], ['data-1', 'data-2'])
            self.assertEqual(len({row['path'] for row in figure_rows}), 2)
            self.assertTrue(all((delivered.root / row['path']).is_file() for row in figure_rows))
            self.assertNotIn('Supplied Descriptive Data', delivered.read_text('report_body.md'))

    def test_import_regenerates_figures_and_rebuild_retains_configured_input_limit(self):
        from simple_ar.result_analysis.table import copy_analysis_package
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.package(root)
            payload = json.loads(path.read_text(encoding='utf-8'))
            payload['figures'] = [{'path': '../../../untrusted.svg', 'caption': 'Run arbitrary code'}]
            path.write_text(json.dumps(payload), encoding='utf-8')
            result = copy_analysis_package(path, root / 'imported')
            self.assertNotIn('untrusted', json.dumps(result['figures']))
            with patch('simple_ar.result_analysis.table.load_analysis_package', wraps=load_analysis_package) as loader:
                rebuild(root / 'imported/analysis.json')
                self.assertEqual(loader.call_args.kwargs, {'max_mb': None})

    @unittest.skipUnless(os.environ.get('SAR_TEST_EXPORT_INTEGRATION') == '1' and
        all(shutil.which(name) for name in ('pandoc', 'rsvg-convert', 'pdflatex', 'bibtex')),
        'Opt-in server check requires the existing export toolchain')
    def test_data_appendix_exports_and_moved_acm_engineering_recompiles(self):
        from simple_ar.report.export import export_acm_report, _compile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = self.package(root)
            inputs = ArtifactStore(path.parent)
            ref = inputs.ref(path.name)
            report = ArtifactStore(root / 'report')
            run_report_capability(context=CapabilityContext(store=report, input_store=inputs, inputs=(ref,),
                attempt=AttemptManifest(attempt_id='report-1', capability='report')),
                request=ReportAssemblyRequest(title='Descriptive data delivery', sections=(ReportSectionDraft(
                    section_id='discussion', heading='Discussion',
                    draft_markdown='Supplied data only; no independently repeated experiment.'),), table_analyses=(ref,)))
            exported = root / 'export'
            manifest = export_acm_report(report.root, exported, compile_pdf=True)
            self.assertTrue(manifest['compiled'], manifest)
            self.assertTrue((exported / 'figures/figure-1.svg').is_file())
            self.assertIn('Supplied Descriptive Data', (exported / 'source.md').read_text(encoding='utf-8'))
            moved = root / 'moved acm with spaces'
            exported.rename(moved)
            report.root.rename(root / 'hidden-report')
            (root / 'data-session').rename(root / 'hidden-data-session')
            rebuilt = _compile(moved, has_citations=False)
            self.assertTrue(rebuilt['compiled'], (rebuilt, (moved / 'build.log').read_text(encoding='utf-8')[-2200:]))
