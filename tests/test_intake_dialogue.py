import contextlib
import io
import json
from io import BytesIO
from pathlib import Path
import tempfile
import tomllib
import sys
import unittest
from unittest.mock import patch

from simple_ar.cli.intake_dialogue import discuss_start, validate_proposal
from simple_ar.cli.parser import build_parser
from simple_ar.cli.research_config import research_defaults
from simple_ar.cli.start import prepare_start
from simple_ar.core.artifacts import read_json


class Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def ask_json(self, system, payload, **kwargs):
        self.requests.append(json.loads(payload))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def proposal(kind="data_analysis", **changes):
    return {"kind": kind, "summary": "Describe matched observations without inferential claims",
            "questions": [], "assumptions": [], "options": {}, **changes}


class AnalysisReportProposalTests(unittest.TestCase):
    def test_report_can_propose_local_dictionary_without_online_scope(self):
        args = build_parser().parse_args(['start'])
        assets = [{'role': 'material', 'path_quote': 'dictionary.md'},
                  {'role': 'paper', 'path_quote': 'reference.pdf'}]
        result = validate_proposal(proposal(assets=assets, options={'with_report': True, 'sources': 'materials'}), args, set())
        self.assertEqual(result['assets'], assets)
        with self.assertRaisesRegex(ValueError, 'selected function'):
            validate_proposal(proposal(assets=assets), args, set())
        with self.assertRaisesRegex(ValueError, 'supplied material only'):
            validate_proposal(proposal(assets=assets, options={'with_report': True, 'sources': 'search'}), args, set())

    def test_report_intent_is_boolean_confirmed_and_cannot_change_explicit_choice(self):
        args = build_parser().parse_args(['start', '--with-report'])
        result = validate_proposal(proposal(options={'with_report': True}), args, {'with_report'})
        self.assertTrue(result['options']['with_report'])
        with self.assertRaisesRegex(ValueError, 'override explicit'):
            validate_proposal(proposal(options={'with_report': False}), args, {'with_report'})
        with self.assertRaisesRegex(ValueError, 'boolean'):
            validate_proposal(proposal(options={'with_report': 'true'}), args, set())
        with self.assertRaisesRegex(ValueError, 'data_analysis'):
            validate_proposal(proposal('bug_fix', options={'with_report': True}), args, set())


class IntakeDialogueTests(unittest.TestCase):
    def setUp(self):
        # Offline dialogue fixtures must not consume the host's model catalog.
        catalog = patch('simple_ar.integrations.model_profiles.load_model_catalog', return_value=None)
        catalog.start()
        self.addCleanup(catalog.stop)

    def test_supplied_paper_url_becomes_attributed_excerpt_and_prepared_material_with_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            url, repository = 'https://example.test/paper', 'https://github.com/example/research'
            args = self.args(root, '--kind', 'reproduction', '--document', url, '--fulltext',
                '--hypothesis', 'Supplied claim', '--dataset', 'User data', '--expected-outcome', 'Compare score',
                '--metric', 'score', '--command', 'python', 'run.py')
            args.goal = 'Inspect the paper at ' + url
            response = BytesIO(('<html><p>Implementation: ' + repository + '</p><p>' + 'x' * 9000 + '</p></html>').encode())
            response.headers = {'Content-Type': 'text/html'}
            self.assertEqual(args.document, [url])
            client = Client(proposal('reproduction'))
            with patch('simple_ar.research.documents.fulltext.urllib.request.urlopen', return_value=response) as fetch:
                resolved = self.converse(args, client, ['y', 'y'])
            fetch.assert_called_once()
            facts = client.requests[0]['assets']  # Source acquired before the first proposal.
            preview = facts['paper_previews'][0]
            self.assertEqual(preview['source_url'], url)
            self.assertEqual(len(preview['text']), 8000)
            self.assertTrue(preview['text_truncated'])
            self.assertIn('not a full-paper', preview['limitations'])
            manifest = read_json(resolved._start_root / 'paper_acquisitions' / '1' / 'fulltext_manifest.json')
            self.assertEqual(manifest['budget']['max_fulltext_documents'], 1)
            self.assertEqual(manifest['budget']['max_fulltext_fetch_attempts'], 1)
            self.assertEqual(manifest['budget']['max_pdf_mb'], 20)
            self.assertTrue(manifest['allow_pdf_download'])
            self.assertTrue(manifest['budget']['keep_raw_pdf'])
            request = {'role': 'project', 'url': repository,
                       'basis': [{'path': preview['source_path'], 'quote': repository}]}
            self.assertIsNone(resolved.project)
            self.assertIsNone(resolved.cwd)
            validate_proposal(proposal('reproduction', acquisition_proposal=request), resolved, set(), facts=facts)
            with patch.object(resolved, 'cwd', root), self.assertRaisesRegex(ValueError, 'cannot be replaced'):
                validate_proposal(proposal('reproduction', acquisition_proposal=request), resolved, set(), facts=facts)
            with self.assertRaisesRegex(ValueError, 'does not match'):
                validate_proposal(proposal('reproduction', acquisition_proposal=request), resolved, set(), facts={})
            resumed = self.args(root, '--resume-setup', str(resolved._start_root))
            with patch('simple_ar.research.documents.fulltext.urllib.request.urlopen') as fetch:
                restored = self.converse(resumed, Client(), [])
            fetch.assert_not_called()
            self.assertEqual(restored.document, resolved.document)
            self.assertEqual(restored.paper_sources, resolved.paper_sources)
            self.assertFalse(resolved.fulltext)
            self.assertFalse(restored.fulltext)
            # Follow an inspected official page through the same document owner,
            # without adopting it as executable project/data or refetching on restore.
            from simple_ar.cli.intake_dialogue import _adopt_acquisition
            from simple_ar.core.budget import BudgetLedger
            instructions = 'https://example.test/instructions'
            supporting = {'role': 'material', 'url': instructions,
                          'basis': [{'path': preview['source_path'], 'quote': instructions}]}
            extended_facts = {**facts, 'material_excerpts': [
                {'path': preview['source_path'], 'text': 'Official instructions: ' + instructions}]}
            validated = validate_proposal(proposal('reproduction', acquisition_proposal=supporting),
                                          resolved, set(), facts=extended_facts)
            acquired = {'request': validated['acquisition_proposal'], 'approved': True,
                        'reply': 'y', 'allow_pdf_download': False}
            support_root = root / 'support-setup'
            support_root.mkdir()
            state = {'acquisitions': [acquired], 'paper_acquisitions': []}
            ledger = BudgetLedger(storage_path=support_root / 'setup_budget.json')
            response = BytesIO(b'<html><p>Official entry: python example.py</p></html>')
            response.headers = {'Content-Type': 'text/html'}
            with patch('simple_ar.research.preparation_assets.public_document_response', return_value=response) as fetch:
                self.assertTrue(_adopt_acquisition(resolved, state, acquired, root=support_root, ledger=ledger))
                self.assertTrue(_adopt_acquisition(resolved, state, acquired, root=support_root, ledger=ledger))
            fetch.assert_called_once()
            self.assertEqual(fetch.call_args.args, (instructions,))
            self.assertEqual(fetch.call_args.kwargs['max_bytes'], 20 * 1024 * 1024)
            self.assertTrue(callable(fetch.call_args.kwargs['on_redirect']))
            self.assertEqual(len(ledger.entries), 1)
            self.assertEqual(ledger.entries[0].status, 'settled')
            self.assertEqual(ledger.entries[0].actual['download_requests'], 1)
            self.assertIsNone(resolved.project)
            self.assertIn(instructions, resolved.paper_sources.values())
            resolved.cwd = root  # Separate prepared-command configuration, after acquisition proposal checks.
            resolved.prepare_only = True
            with patch('sys.stdin.isatty', return_value=False), patch('simple_ar.cli.start.print_line'):
                config = prepare_start(resolved)
            values = research_defaults(['research-session', '--config', str(config)])
            self.assertEqual(values['task_kind'], 'reproduction')
            self.assertEqual(values['command_argv'], ['python', 'run.py'])
            self.assertFalse((root / 'run.py').exists())  # Neither install nor execution.

    def test_paper_url_requires_read_consent_and_redirected_pdf_requires_separate_permission(self):
        url = 'https://example.test/paper?id=public-paper-17&version=2'
        from simple_ar.research.preparation_assets import validate_public_url
        self.assertEqual(validate_public_url(url), url)
        for private in ('?api_key=secret', '?access-token=secret', '?X-Amz-Signature=secret'):
            with self.subTest(private=private), self.assertRaises(ValueError):
                validate_public_url('https://example.test/paper' + private)
        for approved in (False, True):
            with self.subTest(approved=approved), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                args = self.args(root, '--kind', 'reproduction')
                args.goal = 'Inspect ' + url
                client = Client(proposal('reproduction', assets=[{'role': 'paper', 'path_quote': url}]),
                                proposal('reproduction', questions=['Please supply local paper material']))
                response = BytesIO(b'%PDF-1.7\nnot a complete PDF')
                response.headers = {'Content-Type': 'application/pdf'}
                response.geturl = lambda: 'https://example.test/paper.pdf'
                with patch('simple_ar.research.documents.fulltext.urllib.request.urlopen', return_value=response) as fetch:
                    self.assertIsNone(self.converse(args, client, ['y', 'n'] if approved else ['n', 'stop']))
                self.assertEqual(fetch.call_count, int(approved))
                state = read_json(self.draft(root))
                self.assertEqual(state['arguments']['document'], [])
                if approved:
                    entry = state['paper_acquisitions'][0]
                    self.assertFalse(entry['allow_pdf_download'])
                    manifest = read_json(self.draft(root).parent / 'paper_acquisitions' / '1' / 'fulltext_manifest.json')
                    self.assertEqual(manifest['documents'][0]['hints'][0]['reason'], 'pdf_download_disabled')
                    resumed = self.args(root, '--resume-setup', str(self.draft(root).parent))
                    with patch('simple_ar.research.documents.fulltext.urllib.request.urlopen') as retry:
                        self.assertIsNone(self.converse(resumed, Client(), ['n']))
                    retry.assert_not_called()

    def test_paper_url_interruption_is_retained_without_refetch(self):
        from simple_ar.cli.intake_dialogue import _adopt_paper_url
        from simple_ar.core.artifacts import write_json
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = self.args(root, '--kind', 'reproduction')
            entry = {'url': 'https://example.test/paper', 'allow_pdf_download': False}
            state = {'paper_acquisitions': [entry]}
            write_json(root / 'paper_acquisitions' / '1' / 'receipt.json', {**entry, 'status': 'started'})
            with patch('simple_ar.research.documents.fulltext.urllib.request.urlopen') as fetch, \
                 patch('simple_ar.cli.intake_dialogue.print_line'), patch('builtins.input', return_value='n'):
                self.assertFalse(_adopt_paper_url(args, state, entry, root=root))
            fetch.assert_not_called()
            self.assertEqual(entry['receipt']['status'], 'started')
            self.assertEqual(args.document, [])
            receipt = (root / 'paper_acquisitions' / '1' / 'receipt.json').read_bytes()
            with patch('simple_ar.research.documents.fulltext.urllib.request.urlopen') as fetch, \
                 patch('simple_ar.cli.intake_dialogue.print_line'), patch('builtins.input', return_value='y'):
                self.assertTrue(_adopt_paper_url(args, state, entry, root=root))
                self.assertTrue(_adopt_paper_url(args, state, entry, root=root))
            fetch.assert_not_called()
            self.assertTrue(entry['skipped'])
            self.assertFalse(entry.get('adopted', False))
            self.assertEqual(args.document, [])
            self.assertEqual((root / 'paper_acquisitions' / '1' / 'receipt.json').read_bytes(), receipt)
            self.assertIn('not source evidence', state['user_messages'][-1])

    def test_method_figure_dialogue_requires_no_fabricated_dataset(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = self.args(root, '--kind', 'figure')
            args.goal = 'Draw observations → comparison → evidence, as an editable method diagram, not measured results.'
            resolved = self.converse(args, Client(proposal('figure')), ['y'])
            self.assertEqual(resolved.kind, 'figure')
            self.assertIsNone(resolved.data_file)
            self.assertEqual(resolved.goal, args.goal)
            with self.assertRaisesRegex(ValueError, 'Data semantics'):
                validate_proposal(proposal('figure', options={'value_column': ['invented']}), args, set())

    def test_custom_analysis_technical_setup_cannot_recreate_retired_scaffold(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'measurements.csv'
            data.write_text('method,cost,accuracy\nA,1,70\nB,3,78\n')
            args = self.args(root, '--kind', 'data_analysis', '--data-file', str(data), locked={'kind'})
            args.goal = 'Draw cost/accuracy and a custom method diagram in one figure. Rows are method summaries, not repeated trials.'
            client = Client(proposal('data_analysis', options={'scripted': True,
                'observation_unit': 'method summary', 'value_unit': 'accuracy percent; cost seconds'}))
            resolved = self.converse(args, client, ['y'])
            self.assertTrue(resolved.scripted)
            contract = client.requests[0]['response_contract']
            self.assertNotIn('scripted', contract['options'])
            self.assertIn('with_report', contract['options'])
            self.assertNotIn('scripted=true', contract['native_paired_analysis'])
            self.assertIn('ordinary start', contract['native_paired_analysis'])
            self.assertNotIn('execution_proposal', contract)
            self.assertEqual(resolved.goal, args.goal)
            self.assertEqual(resolved.value_column, [])
            no_calls = Client()
            resumed = self.converse(self.args(root, '--resume-setup', str(resolved._start_root)), no_calls, [])
            self.assertTrue(resumed.scripted)
            self.assertEqual(resumed.goal, args.goal)
            self.assertEqual(no_calls.requests, [])
            resolved.prepare_only = True
            retained = (resolved._start_root / 'setup.json').read_bytes()
            with patch('sys.stdin.isatty', return_value=False), patch('simple_ar.cli.start.print_line'), \
                    self.assertRaisesRegex(ValueError, 'chat/resume_setup structured controls'):
                prepare_start(resolved)
            self.assertEqual((resolved._start_root / 'setup.json').read_bytes(), retained)
            self.assertFalse((resolved._start_root / 'code_task.toml').exists())
            with self.assertRaisesRegex(ValueError, 'cannot combine'):
                validate_proposal(proposal('data_analysis', options={'scripted': True, 'with_report': True}), args, set())
            with self.assertRaisesRegex(ValueError, 'explicit scripted'):
                validate_proposal(proposal('data_analysis', options={'scripted': True}), args, {'scripted'})

    def test_public_acquisition_requires_source_basis_and_separate_permission(self):
        from simple_ar.cli.intake_dialogue import _discover_sources
        from simple_ar.research.sources.capability import SearchResult
        from simple_ar.literature.models import Paper
        args = build_parser().parse_args(['start', '--kind', 'reproduction'])
        queries = ['Find a public paper copy and author implementation']
        self.assertEqual(validate_proposal(proposal('reproduction', source_queries=queries),
                                          args, set())['source_queries'], queries)
        for changes in ({'kind': 'writing'}, {'assets': [{'role': 'paper', 'path_quote': 'paper.pdf'}]},
                        {'source_queries': ['']}, {'source_queries': queries * 3}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_proposal({**proposal('reproduction', source_queries=queries), **changes}, args, set())
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = SearchResult(status='partial', responses=(), diagnostics=('One provider unavailable',),
                papers=(Paper(id='source', title='Public source', authors=[], abstract='Metadata only',
                              url='https://example.org/paper', fulltext_url='https://example.org/paper.pdf'),))
            state = {}
            with patch('simple_ar.research.sources.capability.search_sources', return_value=result) as search, \
                 patch('simple_ar.cli.intake_dialogue.print_line'), patch('builtins.input', return_value='n'):
                self.assertFalse(_discover_sources(queries, args, state, root))
                search.assert_not_called()
            with patch('simple_ar.research.sources.capability.search_sources', return_value=result) as search, \
                 patch('simple_ar.cli.intake_dialogue.print_line'), patch('builtins.input', return_value='y'):
                self.assertTrue(_discover_sources(queries, args, state, root))
                self.assertEqual(search.call_count, 1)
                self.assertFalse(_discover_sources(queries, args, state, root))
                args.material = []  # Resume after saved response, before material adoption.
                self.assertTrue(_discover_sources(queries, args, state, root))
                self.assertEqual(search.call_count, 1)
            metadata = args.material[0].read_text(encoding='utf-8')
            self.assertIn('https://example.org/paper.pdf', metadata)
            self.assertIn('One provider unavailable', metadata)
            self.assertEqual(args.document, [])
            args.material = []
            (Path(state['source_lookups'][0]['path']) / 'responses.json').unlink()
            with patch('simple_ar.research.sources.capability.search_sources') as search, \
                 patch('simple_ar.cli.intake_dialogue.print_line'):
                self.assertFalse(_discover_sources(queries, args, state, root))
                search.assert_not_called()
        url = 'https://github.com/example/research'
        request = {'role': 'project', 'url': url, 'basis': [{'path': 'paper.md', 'quote': url}]}
        facts = {'paper_previews': [{'source_path': 'paper.md', 'text': 'Implementation: ' + url}]}
        validated = validate_proposal(proposal('reproduction', acquisition_proposal=request), args, set(), facts=facts)
        self.assertEqual(validated['acquisition_proposal'], request)
        selected = validate_proposal(proposal('reproduction', acquisition_proposal={**request,
            'basis': [{'path': 'paper.md'}]}), args, set(), facts=facts)
        self.assertEqual(selected['acquisition_proposal'], request)
        with self.assertRaisesRegex(ValueError, 'Acquisition URL'):
            validate_proposal(proposal('reproduction', acquisition_proposal={**request,
                'url': url + '-other', 'basis': [{'path': 'paper.md'}]}), args, set(), facts=facts)
        wrapped = {'paper_previews': [{'source_path': 'paper.md',
            'text': 'Footnote: https://github.com/example/\nresearch\nPDF hyperlink targets: ' + url}]}
        selected = validate_proposal(proposal('reproduction', acquisition_proposal={**request,
            'basis': [{'path': 'paper.md', 'quote': 'https://github.com/example/\nresearch'}]}),
            args, set(), facts=wrapped)
        self.assertEqual(selected['acquisition_proposal'], request)
        for changes in ({'url': 'http://example.org/archive.zip'}, {'url': 'https://user:secret@example.org/archive.zip'},
                        {'destination': '/outside'}, {'role': 'script'}, {'basis': [{'path': 'unread.md', 'quote': url}]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_proposal(proposal('reproduction', acquisition_proposal={**request, **changes}), args, set(), facts=facts)
        with self.assertRaisesRegex(ValueError, 'before proposing'):
            validate_proposal(proposal('reproduction', acquisition_proposal=request,
                execution_proposal={}), args, set(), facts=facts)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paper = root / 'paper.md'
            paper.write_text('Implementation: ' + url)
            args = self.args(root, '--kind', 'reproduction', '--document', str(paper))
            acquisition = {**request, 'basis': [{'path': str(paper.resolve()), 'quote': url}]}
            with patch('simple_ar.research.preparation_assets.acquire_asset') as acquire:
                self.assertIsNone(self.converse(args, Client(proposal('reproduction', acquisition_proposal=acquisition)), ['stop']))
            acquire.assert_not_called()

    def test_acquired_project_is_inspected_then_proposes_command_in_the_same_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paper = root / 'paper.md'
            url = 'https://github.com/example/research'
            paper.write_text('Constructed fixture conclusion: score=3. Implementation: ' + url)
            args = self.args(root, '--kind', 'reproduction', '--document', str(paper))
            acquisition = {'role': 'project', 'url': url, 'basis': [{'path': str(paper.resolve()), 'quote': url}]}
            execution = {'hypothesis': 'Check the supplied fixture conclusion', 'dataset': 'Generated fixture only',
                'expected_outcome': 'score=3', 'metrics': ['score'], 'argv': ['python', 'run.py'],
                'basis': [{'path': 'README.md', 'quote': 'python run.py'}]}
            args.asset_max_mb = 32
            def acquire(*, root, role, url, ledger, max_download_mb):
                self.assertEqual(max_download_mb, 32)
                project = root / 'project'
                project.mkdir(parents=True)
                (project / 'README.md').write_text('Run the fixture: python run.py\n')
                (project / 'run.py').write_text('raise AssertionError("Setup must not execute this file")\n')
                return {'status': 'completed', 'path': str(project), 'url': url, 'role': role}
            client = Client(proposal('reproduction', acquisition_proposal=acquisition),
                            proposal('reproduction', execution_proposal=execution))
            with patch('simple_ar.research.preparation_assets.acquire_asset', side_effect=acquire) as download:
                resolved = self.converse(args, client, ['y', 'y'])
            self.assertEqual(download.call_count, 1)
            self.assertIn('project_preparation', client.requests[1]['assets'])
            self.assertEqual(resolved.run_argv, ['python', 'run.py'])
            self.assertTrue((resolved.project / 'README.md').is_file())
            saved = read_json(resolved._start_root / 'setup.json')
            self.assertTrue(saved['acquisitions'][0]['adopted'])
            self.assertEqual(saved['acquisitions'][0]['max_download_mb'], 32)
            # Accepted setup restoration does not fetch or ask the model again.
            resumed = self.args(root, '--resume-setup', str(resolved._start_root))
            with patch('simple_ar.research.preparation_assets.acquire_asset') as download:
                restored = self.converse(resumed, Client(), [])
            download.assert_not_called()
            self.assertEqual(restored.project, resolved.project)
            self.assertEqual(restored.asset_max_mb, 32)
            resolved.prepare_only = True
            with patch('sys.stdin.isatty', return_value=False), patch('simple_ar.cli.start.print_line'):
                config = prepare_start(resolved)
            settings = research_defaults(['research-session', '--config', str(config)])
            self.assertEqual(settings['cwd'], str(resolved.project))
            self.assertEqual(settings['command_argv'], ['python', 'run.py'])
            # A configured setup points to the saved config instead of preparing it again.
            with patch('simple_ar.research.preparation_assets.acquire_asset') as download:
                self.assertIsNone(self.converse(resumed, Client(), []))
            download.assert_not_called()

    def test_paper_content_is_previewed_only_after_existing_asset_confirmation(self):
        for kind in ('reproduction', 'writing'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                paper = root / 'paper.md'
                paper.write_text('# Conclusion\nThe measured mean is three under the fixed split.\n')
                args = self.args(root, '--kind', kind)
                args.goal = f'Use the conclusion in {paper}'
                client = Client(proposal(kind, assets=[{'role': 'paper', 'path_quote': str(paper)}]), proposal(kind))
                resolved = self.converse(args, client, ['y', 'y'])
                self.assertNotIn('paper_previews', client.requests[0]['assets'])
                preview = client.requests[1]['assets']['paper_previews'][0]
                self.assertIn('mean is three', preview['text'])
                self.assertEqual(preview['source_path'], str(paper.resolve()))
                self.assertEqual(preview['parser'], 'plain_text')
                self.assertEqual(preview['status'], 'bounded_preview')
                self.assertEqual(resolved.document, [paper.resolve()])
                self.assertEqual(len(client.requests), 2)

    def test_paper_previews_bound_text_and_document_count_without_following_links(self):
        from simple_ar.cli.intake_dialogue import _asset_preview
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            other = root / 'unconfirmed.txt'
            other.write_text('Unconfirmed content must not be disclosed.')
            papers = [root / f'paper-{index}.md' for index in range(5)]
            for paper in papers:
                paper.write_text(f'# Claim\n[Other material]({other})\n' + 'x' * 8000 + 'UNSHOWN TAIL')
            args = self.args(root, '--kind', 'reproduction')
            args.document = papers
            with patch('urllib.request.urlopen', side_effect=AssertionError('no network')):
                facts = _asset_preview(args)
            self.assertEqual(len(facts['paper_previews']), 4)
            self.assertEqual(facts['paper_preview_limits']['omitted_documents'], 1)
            for row, paper in zip(facts['paper_previews'], papers):
                self.assertEqual(row['source_path'], str(paper.resolve()))
                self.assertEqual(row['extracted_text_character_range'], [0, 8000])
                self.assertTrue(row['text_truncated'])
                self.assertNotIn('UNSHOWN TAIL', row['text'])
                self.assertNotIn('must not be disclosed', row['text'])
            material = root / 'observations.json'
            material.write_text('{"status":"partial","observations":[0.2,0.4]}')
            args.document = [material]
            rejected = _asset_preview(args)['paper_previews'][0]
            self.assertEqual(rejected['reason'], 'unsupported_document_suffix')
            args.document = []
            args.material = [material]
            facts = _asset_preview(args, saved_previews=[rejected])
            view = facts['paper_previews'][0]
            self.assertEqual(view['input_role'], 'material')
            self.assertIn('"status":"partial"', view['text'])
            self.assertIn('not a full-paper reading or verified result', view['limitations'])
            validate_proposal(proposal('reproduction', material_read_requests=[{
                'path': str(material.resolve()), 'query': 'Which observations are recorded?'}]),
                args, set(), facts=facts)

    def test_pdf_preview_uses_document_parser_and_preserves_page_coverage_and_failure(self):
        from simple_ar.cli.intake_dialogue import _asset_preview
        from simple_ar.research.documents.ports import ParsedDocument
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paper = root / 'paper.pdf'
            paper.write_bytes(b'%PDF-1.7\nfixture')
            args = self.args(root, '--document', str(paper))
            coverage = {'total_pages': 9, 'extracted_pages': 3, 'page_limit': 3,
                        'truncated': True, 'empty_text_pages': [2]}
            with patch('simple_ar.research.documents.extractors.LocalDocumentParser') as parser:
                parser.return_value.parse.return_value = ParsedDocument('Observed conclusion', 'pypdf_optional', coverage)
                preview = _asset_preview(args)['paper_previews'][0]
                parser.assert_called_once_with(max_pdf_pages=3)
                parser.return_value.parse.assert_called_once_with(paper.resolve())
                self.assertEqual(preview['coverage'], coverage)
                self.assertFalse(preview['text_truncated'])  # Text fits; later PDF pages remain unread.
                self.assertEqual(preview['extracted_text_character_range'], [0, 19])
                parser.return_value.parse.side_effect = RuntimeError('PDF parser unavailable')
                failed = _asset_preview(args)['paper_previews'][0]
                self.assertEqual(failed['status'], 'unavailable')
                self.assertEqual(failed['reason'], 'preview_parse_failed:RuntimeError')
                self.assertNotIn('text', failed)

            # Question-directed reading keeps the initial preview unchanged,
            # then parses only confirmed local inputs into recoverable bundles.
            from simple_ar.research.documents.extractors import LocalDocumentParser
            from simple_ar.research.documents.ingest import DocumentBundle
            late = root / 'notes.txt'
            repository = 'https://github.com/example/late-method'
            late.write_text('Early introduction.\n' * 900 + '\n# Data availability\n' + repository + '\n')
            queries = [{'path': str(paper.resolve()), 'query': 'Methods sample size'},
                       {'path': str(late.resolve()), 'query': 'Data availability ' + repository}]
            original_parser = LocalDocumentParser
            limits = []
            def parser_factory(*, max_pdf_pages):
                limits.append(max_pdf_pages)
                parser = original_parser(max_pdf_pages=max_pdf_pages)
                original_parse = parser.parse
                def parse(path):
                    if path.suffix == '.pdf':
                        if max_pdf_pages == 3:
                            return ParsedDocument('Observed conclusion', 'pypdf_optional', coverage)
                        return ParsedDocument('Introduction.\n' * 900 + '\n# Methods\nSample size is 17.\n',
                            'pypdf_optional', {'total_pages': 61, 'extracted_pages': 40,
                                'page_limit': 40, 'truncated': True, 'empty_text_pages': []})
                    return original_parse(path)
                parser.parse = parse
                return parser
            args = self.args(root, '--kind', 'writing', '--document', str(paper), '--material', str(late))
            client = Client(proposal('writing', material_read_requests=queries), RuntimeError('Stop before next proposal'))
            with patch('simple_ar.research.documents.extractors.LocalDocumentParser', side_effect=parser_factory), \
                 self.assertRaisesRegex(RuntimeError, 'Stop before next proposal'):
                self.converse(args, client, ['y'])  # One grouped expansion consent.
            setup = self.draft(root)
            state = read_json(setup)
            self.assertEqual(limits, [3, 40, 40])
            self.assertEqual(state['paper_previews'][0]['coverage'], coverage)
            self.assertNotIn(repository, state['paper_previews'][1]['text'])
            facts = client.requests[1]['assets']
            excerpts = facts['material_excerpts']
            self.assertIn('Sample size is 17', '\n'.join(row['text'] for row in excerpts))
            self.assertIn(repository, '\n'.join(row['text'] for row in excerpts))
            self.assertLessEqual(sum(len(row['text']) for row in excerpts), 8000)
            self.assertTrue(facts['material_read_results'][0]['coverage'][0]['coverage']['truncated'])
            self.assertNotIn('chunks', facts)
            for entry in state['material_reads']:
                bundle = DocumentBundle.from_handoff_dict(read_json(setup.parent / entry['bundle']))
                self.assertTrue(bundle.chunks)
            reproduction = self.args(root, '--kind', 'reproduction')
            validate_proposal(proposal('reproduction', acquisition_proposal={'role': 'project', 'url': repository,
                'basis': [{'path': str(late.resolve()), 'quote': repository}]}), reproduction, set(), facts=facts)
            reproduction.project = root
            with self.assertRaisesRegex(ValueError, 'does not match'):
                validate_proposal(proposal('reproduction', execution_proposal={
                    'argv': ['python', 'run.py'], 'metrics': ['score'], 'hypothesis': 'Fixed claim',
                    'dataset': 'Fixed data', 'expected_outcome': 'Check score',
                    'basis': [{'path': str(late.resolve()), 'quote': repository}]}), reproduction, set(), facts=facts)
            for requests in ([{'path': str(root / 'outside.txt'), 'query': 'Methods'}],
                             [{'path': str(paper), 'query': ''}], queries * 2):
                with self.subTest(requests=requests), self.assertRaises(ValueError):
                    validate_proposal(proposal('writing', material_read_requests=requests), args, set(), facts=facts)
            with self.assertRaisesRegex(ValueError, 'cannot combine'):
                validate_proposal(proposal('writing', material_read_requests=queries, assets=[
                    {'role': 'material', 'path_quote': 'another.txt'}]), args, set(), facts=facts)
            resumed = Client(proposal('writing', material_read_requests=queries),
                proposal('writing', material_read_requests=queries))
            with patch('simple_ar.research.documents.extractors.LocalDocumentParser', side_effect=AssertionError('no reparse')):
                self.assertIsNone(self.converse(self.args(root, '--resume-setup', str(setup.parent)), resumed, []))
                self.assertEqual(read_json(setup)['last_read_result']['status'], 'no_progress')
                restored = self.converse(self.args(root, '--resume-setup', str(setup.parent), '--goal', 'Clarify without rereading', locked=('goal',)),
                    Client(proposal('writing')), ['y'])
            self.assertEqual(restored.material, [late.resolve()])

    def test_invalid_proposals_resume_without_requiring_user_configuration_repair(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bad = proposal('survey', options={'data_mode': 'observations'})
            good = proposal('survey', options={'sources': 'search'})
            client = Client(bad, bad)
            self.assertIsNone(self.converse(self.args(root), client, []))
            setup = self.draft(root)
            self.assertEqual(len(client.requests), 2)
            resumed = Client(good)
            result = self.converse(self.args(root, '--resume-setup', str(setup.parent)), resumed, ['y'])
            self.assertEqual(result.kind, 'survey')
            state = read_json(self.draft(root))
            self.assertEqual(len(state['rejected_proposals']), 2)
            self.assertEqual(len(resumed.requests), 1)

    def test_empty_requirements_retain_current_environment_without_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'paper.md').write_text('Reference')
            args = self.args(root, '--kind', 'reproduction', '--project', str(root), '--document', str(root / 'paper.md'),
                '--project-python', sys.executable,
                '--hypothesis', 'Claim', '--dataset', 'Fixed data', '--expected-outcome', 'Compare score',
                '--metric', 'score', '--command', 'python', 'run.py')
            settings = {'environment': 'current', 'requirements': [], 'install_project': False}
            resolved = self.converse(args, Client(proposal('reproduction', options=settings)), ['y'])
            resolved.prepare_only = True
            with patch('sys.stdin.isatty', return_value=False), patch('simple_ar.cli.start.print_line'):
                config = prepare_start(resolved)
            defaults = research_defaults(['research-session', '--config', str(config)])
            self.assertNotIn('environment', defaults['execution_details'])
            for invalid in ('requirements.txt', [''], [None]):
                with self.assertRaisesRegex(ValueError, 'list of names'):
                    validate_proposal(proposal('reproduction', options={'requirements': invalid}), args, set())
            with self.assertRaisesRegex(ValueError, 'nonempty'):
                validate_proposal(proposal('data_analysis', options={'value_column': []}), args, set())

    def test_confirmed_project_install_reuses_inspected_declaration_and_canonical_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'pyproject.toml').write_text('[project]\nname="fixture"\nversion="0.1"\n')
            (root / 'paper.md').write_text('Supplied reference')
            args = self.args(root, '--kind', 'reproduction', '--project', str(root), '--document', str(root / 'paper.md'),
                '--hypothesis', 'Claim', '--dataset', 'Fixed data', '--expected-outcome', 'Compare observed score',
                '--metric', 'score', '--command', 'python', 'run.py')
            settings = {'environment': 'venv', 'install_project': True}
            client = Client(proposal('reproduction', options=settings))
            resolved = self.converse(args, client, ['y'])
            resolved.prepare_only = True
            with patch('sys.stdin.isatty', return_value=False), patch('simple_ar.cli.start.print_line'):
                config = prepare_start(resolved)
            defaults = research_defaults(['research-session', '--config', str(config)])
            self.assertTrue(defaults['execution_details']['environment']['install_project'])
            self.assertEqual(defaults['process_invocations'], 4)
            facts = client.requests[0]['assets']
            with self.assertRaisesRegex(ValueError, 'override explicit'):
                validate_proposal(proposal('reproduction', options=settings), args, {'install_project'}, facts=facts)
            with self.assertRaisesRegex(ValueError, 'inspected root'):
                validate_proposal(proposal('reproduction', options=settings), args, set())
            with self.assertRaisesRegex(ValueError, 'boolean'):
                validate_proposal(proposal('reproduction', options={'install_project': 'true'}), args, set(), facts=facts)
            with self.assertRaisesRegex(ValueError, 'explicit venv'):
                validate_proposal(proposal('reproduction', options={'install_project': True}), args, set(), facts=facts)
            with self.assertRaisesRegex(ValueError, 'requires reproduction'):
                validate_proposal(proposal('writing', options=settings), args, set(), facts=facts)

    def test_confirmed_venv_choice_uses_inspected_requirements_and_existing_serializer(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'requirements.txt').write_text('numpy==2.0.0\n')
            (root / 'paper.md').write_text('Supplied reference')
            args = self.args(root, '--kind', 'reproduction', '--project', str(root), '--document', str(root / 'paper.md'),
                '--hypothesis', 'Claim', '--dataset', 'Fixed data', '--expected-outcome', 'Compare observed score',
                '--metric', 'score', '--command', 'python', 'run.py')
            settings = {'environment': 'venv', 'requirements': ['requirements.txt']}
            client = Client(proposal('reproduction', options=settings, assumptions=['Install confirmed project requirements in a task venv; build code may execute.']))
            resolved = self.converse(args, client, ['y'])
            resolved.prepare_only = True
            with patch('sys.stdin.isatty', return_value=False), patch('simple_ar.cli.start.print_line'):
                config = prepare_start(resolved)
            defaults = research_defaults(['research-session', '--config', str(config)])
            self.assertEqual(defaults['execution_details']['environment']['requirements'], ['requirements.txt'])
            self.assertEqual(defaults['execution_details']['environment']['python_executable'], str(Path(sys.executable).absolute()))
            self.assertEqual(defaults['process_invocations'], 4)
            with self.assertRaisesRegex(ValueError, 'override explicit'):
                validate_proposal(proposal('reproduction', options=settings), args, {'environment'})
            facts = client.requests[0]['assets']
            with self.assertRaisesRegex(ValueError, 'inspected requirements'):
                validate_proposal(proposal('reproduction', options={'environment': 'venv', 'requirements': ['missing.txt']}), args, set(), facts=facts)
            with self.assertRaisesRegex(ValueError, 'requires reproduction'):
                validate_proposal(proposal('writing', options={'environment': 'venv'}), args, set())
    def test_reproduction_data_directory_flows_from_conversation_to_preparation_and_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'shared data'
            data.mkdir()
            (data / 'records.bin').write_bytes(b'not a table or trusted instructions')
            (root / 'README.md').write_text('Run python run.py --data DATA\n')
            paper = root / 'paper.md'
            paper.write_text('A supplied conclusion; no measured result yet.')
            args = self.args(root, '--kind', 'reproduction', '--project', str(root), '--document', str(paper))
            args.goal = f'Check this conclusion with the data at {data}'
            execution = {'hypothesis': 'Check source conclusion', 'dataset': 'Supplied binary directory; split unknown',
                         'expected_outcome': 'Compare declared metric', 'metrics': ['score'],
                         'argv': ['python', 'run.py', '--data', str(data)],
                         'basis': [{'path': 'README.md', 'quote': 'python run.py --data DATA'}]}
            client = Client(proposal('reproduction', assets=[{'role': 'data', 'path_quote': str(data)}]),
                            proposal('reproduction', execution_proposal=execution))
            with patch('subprocess.run', side_effect=AssertionError('intake never executes')):
                resolved = self.converse(args, client, ['y', 'y'])
                resolved.prepare_only = True
                with patch('sys.stdin.isatty', return_value=False), patch('simple_ar.cli.start.print_line'):
                    config = prepare_start(resolved)
            inspected = client.requests[1]['assets']['project_preparation']['data_paths']
            self.assertEqual(inspected, [{'path': str(data.resolve()), 'available': True, 'kind': 'directory'}])
            self.assertIsNone(resolved.data_file)
            self.assertEqual(resolved.data_path, [data.resolve()])
            defaults = research_defaults(['research-session', '--config', str(config)])
            self.assertEqual(defaults['data_path'], [str(data.resolve())])
            self.assertEqual(defaults['command_argv'], execution['argv'])
            self.assertEqual(read_json(config.parent / 'preparation.json')['data_paths'], inspected)

    def test_reproduction_explicit_data_is_not_reauthorized_or_interpreted_as_table(self):
        from simple_ar.cli.intake_dialogue import _asset_preview
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'large.npz'
            data.write_bytes(b'not parsed')
            args = self.args(root, '--kind', 'reproduction', '--cwd', str(root), '--data-path', str(data))
            parsed = validate_proposal(proposal('reproduction', assets=[{'role': 'data', 'path_quote': str(data)}]), args, set())
            self.assertEqual(parsed['assets'], [])
            with patch('simple_ar.result_analysis.table.read_table_source', side_effect=AssertionError('no table parsing')):
                facts = _asset_preview(args)
            self.assertEqual(facts['project_preparation']['data_paths'][0]['size_bytes'], 10)
            self.assertNotIn('data_preview', facts)

    def test_unselected_function_still_inspects_explicit_execution_directory(self):
        from simple_ar.cli.intake_dialogue import _asset_preview
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'README.md').write_text('Run python run.py with the declared data.')
            args = self.args(root, '--cwd', str(root))
            self.assertIsNone(args.kind)
            with patch('subprocess.run', side_effect=AssertionError('metadata only')):
                facts = _asset_preview(args)
            self.assertEqual(facts['project_preparation']['project'], str(root.resolve()))
            self.assertIn('Run python run.py', facts['project_preparation']['excerpts'][0]['text'])

    def test_old_setup_without_data_paths_recovers_in_shared_dialogue(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = self.args(root, '--kind', 'survey')
            self.converse(args, Client(proposal('survey', questions=['Which scope?'])), ['stop'])
            path = self.draft(root)
            state = read_json(path)
            state['arguments'].pop('data_path')
            state['arguments'].pop('environment')
            state['arguments'].pop('requirements')
            state['arguments'].pop('install_project')
            from simple_ar.core.artifacts import write_json
            write_json(path, state)
            resumed = self.args(root, '--resume-setup', str(path.parent))
            resolved = self.converse(resumed, Client(proposal('survey')), ['Local only', 'y'])
            self.assertEqual(resolved.data_path, [])

    def test_requested_association_enters_same_data_config_and_respects_explicit_none(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'points.csv'
            data.write_text('x,y\n1,2\n2,3\n')
            args = self.args(root, '--data-file', str(data), locked={'data_file'})
            settings = {'value_column': ['y'], 'x_column': 'x', 'data_mode': 'values',
                'data_plot': 'scatter', 'observation_unit': 'one supplied pair', 'data_association': 'pearson'}
            resolved = self.converse(args, Client(proposal(options=settings)), ['y'])
            resolved.prepare_only = True
            with patch('sys.stdin.isatty', return_value=False), patch('simple_ar.cli.start.print_line'):
                config = prepare_start(resolved)
            self.assertEqual(research_defaults(['research-session', '--config', str(config)])['data_association'], 'pearson')
            args.data_association = 'none'
            with self.assertRaisesRegex(ValueError, 'override explicit'):
                validate_proposal(proposal(options=settings), args, {'data_association'})
            with self.assertRaisesRegex(ValueError, 'Data semantics'):
                validate_proposal(proposal('survey', options={'data_association': 'pearson'}), args, set())

    def test_mode_contract_explains_operations_not_the_natural_meaning_of_observations(self):
        from simple_ar.result_analysis.table import TABLE_MODE_DESCRIPTIONS
        with tempfile.TemporaryDirectory() as folder:
            client = Client(proposal('survey', options={'sources': 'search'}))
            self.converse(self.args(Path(folder)), client, ['stop'])
            mode = client.requests[0]['response_contract']['options']['data_mode']
            self.assertEqual(mode['operations'], TABLE_MODE_DESCRIPTIONS)
            self.assertEqual(set(mode['allowed_values']), set(mode['operations']))
            self.assertIn('Aggregate', mode['operations']['observations'])
            self.assertIn('individual observations', mode['operations']['values'])
            mode_default = build_parser().parse_args(['start']).data_mode
            self.assertEqual(mode_default, 'observations')  # Compatibility: no default/config migration.

    def test_complete_data_proposal_serializes_before_confirmation_or_asks_a_real_question(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'data.csv'
            data.write_text('value\n1\n2\n')
            args = self.args(root, '--data-file', str(data))
            incomplete = proposal(options={'value_column': ['value']}, assumptions=['Each row is one measurement'])
            with self.assertRaises(ValueError):
                validate_proposal(incomplete, args, set())
            # A genuinely unresolved semantic choice remains a conversation,
            # rather than being guessed or blocking the incomplete draft.
            pending = {**incomplete, 'questions': ['What does one row represent?'],
                       'options': {'value_column': ['value'], 'observation_unit': None, 'value_unit': None}}
            draft = validate_proposal(pending, args, set())
            self.assertEqual(draft['questions'], pending['questions'])
            self.assertEqual(draft['options'], {'value_column': ['value']})
            self.assertIsNone(pending['options']['observation_unit'])
            # Resolve pending semantics before validating execution combinations.
            coordinate = proposal(options={'data_plot': 'line', 'value_column': ['value'],
                'observation_unit': 'one coordinate', 'data_mode': 'values'}, questions=['Which column is x?'])
            client = Client(coordinate, proposal(options={'value_column': ['value'], 'observation_unit': 'one measurement'}))
            self.assertEqual(self.converse(args, client, ['Compare the values instead.', 'y']).data_plot, 'bar')
            self.assertFalse(read_json(self.draft(root)).get('rejected_proposals'))
            first = proposal(options={'value_column': ['value']})
            corrected = proposal(options={'value_column': ['value'], 'observation_unit': 'one measurement'})
            client = Client(first, corrected)
            resolved = self.converse(args, client, ['y'])
            self.assertEqual(resolved.observation_unit, 'one measurement')
            self.assertEqual(len(client.requests), 2)
            self.assertIn('validation_error', client.requests[-1])

    def test_attribution_is_optional_and_must_come_from_human_not_model(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = self.args(root)
            declared = "Observatory catalogue https://example.org/catalogue release 2025"
            args.goal += "; Data source: " + declared
            resolved = self.converse(args, Client(proposal(options={"data_attribution": declared})), ["y"])
            self.assertEqual(resolved.data_attribution, declared)
        with tempfile.TemporaryDirectory() as folder:
            args = self.args(Path(folder))
            invented = proposal(options={"data_attribution": "Invented university dataset"})
            self.assertIsNone(self.converse(args, Client(invented, invented), ['stop']))
            self.assertIn('human-provided', read_json(self.draft(Path(folder)))['rejected_proposals'][-1]['error'])

    def args(self, root, *options, locked=()):
        args = build_parser().parse_args(["start", "--chat", "--goal", "Describe my measurements",
                                         "--output-root", str(root / "runs"), *options])
        args._explicit_start_destinations = set(locked)
        return args

    def converse(self, args, client, answers):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=answers), \
             patch("simple_ar.cli.intake_dialogue.print_line"):
            return discuss_start(args, client=client)

    def draft(self, root):
        return next((root / "runs").glob("*/setup.json"))

    def test_semantic_clarification_retains_human_words_and_uses_canonical_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / "paired.csv"
            data.write_text("group,old,new\nA,3,2\nA,4,3\n")
            args = self.args(root, "--data-file", str(data), locked={"data_file"})
            settings = {"value_column": ["old", "new"], "group_column": "group", "value_unit": "seconds",
                        "observation_unit": "one matched run", "paired_baseline": "old"}
            client = Client(proposal(questions=["What does each row mean and are timings matched?"]),
                            proposal(options=settings))
            reply = "One row is one matched run; old and new are times in seconds."
            with patch('simple_ar.cli.intake_dialogue.LLMClient.from_env', return_value=client) as factory:
                resolved = self.converse(args, None, [reply, "y"])
            self.assertNotIn('max_output_tokens', factory.call_args.kwargs)
            self.assertEqual(len(client.requests), 2)
            self.assertEqual(client.requests[0]["assets"]["data_preview"]["columns"], ["group", "old", "new"])
            self.assertIn(reply, resolved.goal)
            self.assertNotIn("Describe matched observations without inferential claims", resolved.goal)
            with patch("sys.stdin.isatty", return_value=False), patch("simple_ar.cli.start.print_line"):
                resolved.prepare_only = True  # Testing canonical serialization, not dialogue's no-API promise.
                config = prepare_start(resolved)
            defaults = research_defaults(["research-session", "--config", str(config)])
            self.assertEqual(defaults["paired_baseline"], "old")
            self.assertEqual(defaults["value_column"], ["old", "new"])
            self.assertEqual(read_json(config.parent / "setup.json")["status"], "configured")
            self.assertNotIn("execution", tomllib.loads(config.read_text()))
            # The public adapter adopts the confirmed protocol once, without
            # rediscovering a native preference or reopening technical setup.
            from simple_ar.cli.main import main
            resolved.prepare_only, resolved.yes = False, True
            with patch("sys.stdin.isatty", return_value=False), \
                    patch("simple_ar.cli.intake_dialogue.discuss_start", return_value=resolved) as discussion, \
                    patch("simple_ar.cli.main._run_native_start") as native, \
                    patch("simple_ar.cli.main._print_research_session") as run, \
                    contextlib.redirect_stdout(io.StringIO()):
                main(["start", "--chat", "--goal", args.goal, "--data-file", str(data), "--yes"])
            discussion.assert_called_once()
            native.assert_not_called()
            self.assertEqual(run.call_args.args[0].paired_baseline, defaults["paired_baseline"])
            self.assertEqual(run.call_args.args[0].value_column, defaults["value_column"])
            self.assertEqual(len(client.requests), 2)

    def test_waiting_proposal_resumes_without_model_replay_or_budget_reset(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = self.args(root)
            first = Client(proposal("survey", options={"sources": "search"}, questions=['Prefer this scope or another?']))
            self.assertIsNone(self.converse(args, first, ["stop"]))
            contract = first.requests[0]['response_contract']
            self.assertNotIn('figure', contract['kind'])
            self.assertNotIn('scripted', contract['options'])
            draft = self.draft(root)
            ledger = draft.parent / "setup_budget.json"
            old = ledger.read_bytes()
            resumed = self.args(root, "--resume-setup", str(draft.parent))
            no_calls = Client()
            result = self.converse(resumed, no_calls, ["accept-proposal"])
            self.assertEqual(result.kind, "survey")
            self.assertEqual(len(no_calls.requests), 0)
            self.assertEqual(ledger.read_bytes(), old)
            self.assertEqual(len(read_json(draft)["proposals"]), 1)
            self.assertIn('Unanswered factual questions remain unknown', result.goal)
            self.assertEqual(read_json(draft)['proposals'][0]['questions'], ['Prefer this scope or another?'])
            from simple_ar.core.artifacts import write_json
            stopped = read_json(draft)
            stopped['status'] = 'discussing'
            write_json(draft, stopped)
            budget = read_json(ledger)
            budget['limits']['llm_requests'] = 0
            write_json(ledger, budget)
            before = ledger.read_bytes()
            offline = Client()
            again = self.converse(self.args(root, '--resume-setup', str(draft.parent)), offline,
                                  ['review', 'accept-proposal'])
            self.assertEqual(again.kind, 'survey')
            self.assertEqual(offline.requests, [])
            self.assertEqual(ledger.read_bytes(), before)

    def test_accepting_saved_proposal_does_not_require_model_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.converse(self.args(root), Client(proposal('survey', options={'sources': 'search'})), ['stop'])
            draft = self.draft(root)
            args = self.args(root, '--resume-setup', str(draft.parent))
            with patch('simple_ar.cli.intake_dialogue.LLMClient.from_env', side_effect=AssertionError('offline acceptance')):
                result = self.converse(args, None, ['y'])
            self.assertEqual(result.sources, 'search')

    def test_outstanding_question_does_not_trigger_unchanged_model_retries(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            client = Client(proposal(questions=["Are rows measurements or summaries?"]), proposal())
            result = self.converse(self.args(root), client, ["y", "", "They are measurements.", "y"])
            self.assertEqual(len(client.requests), 2)
            self.assertIn("They are measurements.", result.goal)

    def test_named_asset_access_is_confirmed_separately_from_original_human_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / "supplied.csv"
            data.write_text("value\n1\n2\n")
            original = f"Describe observations from {data}"
            args = self.args(root)
            args.goal = original
            client = Client(proposal(assets=[{"role": "data", "path_quote": str(data)}]),
                            proposal(options={"value_column": ["value"], "observation_unit": "one observation"}))
            result = self.converse(args, client, ["y", "y"])
            self.assertEqual(result.data_file, data.resolve())
            self.assertEqual(result.goal, original)
            self.assertEqual(client.requests[0]["assets"]["data_file"], "")
            self.assertEqual(client.requests[1]["assets"]["data_preview"]["row_count"], 2)
            self.assertTrue(client.requests[1]["asset_decisions"][0]["approved"])

    def test_explicit_change_on_resume_is_retained_and_reconsidered(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.converse(self.args(root), Client(proposal("survey", options={"sources": "search"})), ["stop"])
            draft = self.draft(root)
            args = self.args(root, "--resume-setup", str(draft.parent), "--goal", "Only review the theory",
                             locked={"goal"})
            client = Client(proposal("survey", options={"sources": "search"}))
            result = self.converse(args, client, ["y"])
            self.assertIn("Only review the theory", result.goal)
            self.assertEqual(read_json(draft)["arguments"]["goal"], "Only review the theory")
            self.assertEqual(len(client.requests), 1)

    def test_invalid_second_proposal_and_transport_failure_preserve_the_draft(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            client = Client(proposal(options={"command": "delete everything"}),
                            proposal(options={"timeout_sec": 10000}))
            # A technical proposal error must not demand another human reply.
            self.assertIsNone(self.converse(self.args(root), client, []))
            state = read_json(self.draft(root))
            self.assertEqual(len(state['rejected_proposals']), 2)
            self.assertIn("timeout_sec", state["rejected_proposals"][-1]["response"]["options"])
            self.assertEqual(state["user_messages"], ["Describe my measurements"])
            self.assertEqual(state["status"], "discussing")
            self.assertIn("validation_error", client.requests[1])
            self.assertIn("command", client.requests[1]['validation_error'])
            self.assertIn("Allowed option keys", client.requests[1]['validation_error'])
            self.assertIn("group_column", client.requests[1]['validation_error'])
            from simple_ar.result_analysis.table import TABLE_PLOT_DESCRIPTIONS
            self.assertEqual(client.requests[0]['response_contract']['options']['data_plot']['operations'], TABLE_PLOT_DESCRIPTIONS)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaisesRegex(RuntimeError, "network"):
                self.converse(self.args(root), Client(RuntimeError("network")), [])
            self.assertTrue(self.draft(root).is_file())

    def test_unquoted_asset_is_rejected_before_access(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            malicious = proposal(assets=[{"role": "data", "path_quote": "/etc/passwd"}])
            client = Client(malicious, malicious)
            self.assertIsNone(self.converse(self.args(root), client, ['stop']))
            self.assertIn('verbatim', read_json(self.draft(root))['rejected_proposals'][-1]['error'])
            self.assertEqual(read_json(self.draft(root))["arguments"]["data_file"], None)

    def test_locked_defaults_and_function_specific_options_cannot_be_overridden(self):
        from simple_ar.cli.research_config import normalize_setup_options, setup_option_contract
        baseline = build_parser().parse_args(['start'])
        for kind in ('survey', 'writing', 'reproduction', 'bug_fix', 'figure', 'data_analysis'):
            for key, spec in setup_option_contract(kind).items():
                value = (spec['allowed_values'][0] if 'allowed_values' in spec else
                         {'string': 'meaning', 'boolean': False, 'list of names': ['value']}[spec['type']])
                supplied = {key: value}
                if kind == 'data_analysis' and key == 'sources':
                    supplied['with_report'] = True
                with self.subTest(kind=kind, key=key):
                    self.assertEqual(normalize_setup_options(supplied, baseline, set(), kind), supplied)
        with tempfile.TemporaryDirectory() as folder:
            args = self.args(Path(folder), "--data-missing", "reject")
            unchanged = validate_proposal(proposal(options={'data_missing': None}), args, {'data_missing'})
            self.assertEqual(unchanged['options'], {})
            with self.assertRaisesRegex(ValueError, "explicit data_missing"):
                validate_proposal(proposal(options={"data_missing": "omit"}), args, {"data_missing"})
            with self.assertRaisesRegex(ValueError, "belong"):
                validate_proposal(proposal("writing", assets=[{"role": "data", "path_quote": "x.csv"}]), args, set())
            value = proposal('reproduction', options={'template': 'reproduction'})
            resolved = validate_proposal(value, args, set())
            self.assertEqual(resolved['options'], {})
            self.assertEqual(value['options'], {'template': 'reproduction'})
            with self.assertRaisesRegex(ValueError, 'requires writing'):
                validate_proposal(proposal('reproduction', options={'template': 'experiment'}), args, set())

    def test_prepare_only_cannot_make_a_model_call_and_configured_resume_does_not_replay(self):
        from simple_ar.cli.main import main
        with self.assertRaisesRegex(SystemExit, "--prepare-only promises no model calls"):
            main(["start", "--chat", "--prepare-only", "--kind", "survey", "--goal", "Survey"])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaisesRegex(ValueError, "no model calls"):
                self.converse(self.args(root, "--prepare-only"), Client(), [])
            result = self.converse(self.args(root), Client(proposal("survey", options={"sources": "search"})), ["y"])
            with patch("sys.stdin.isatty", return_value=False), patch("simple_ar.cli.start.print_line"):
                result.prepare_only = True
                config = prepare_start(result)
            self.assertIsNone(self.converse(self.args(root, "--resume-setup", str(config.parent)), Client(), []))

    def test_large_text_cells_are_not_sent_whole_as_data_preview(self):
        from simple_ar.cli.intake_dialogue import _asset_preview
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'wide.csv'
            data.write_text('value,note\n1,' + 'x' * 50000 + '\n')
            facts = _asset_preview(self.args(root, '--data-file', str(data)))['data_preview']
            self.assertTrue(facts['cell_values_truncated'])
            self.assertEqual(len(facts['example_rows'][0]['note']), 200)
            self.assertEqual(facts['row_count'], 1)
            self.assertEqual(len(data.read_text()), 50014)

    def test_repeated_cli_asset_does_not_ask_for_access_twice(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'supplied.csv'
            data.write_text('value\n1\n')
            args = self.args(root, '--data-file', str(data))
            checked = validate_proposal(proposal(assets=[{'role': 'data', 'path_quote': str(data)}],
                questions=['What does one row represent?']), args, set())
            self.assertEqual(checked['assets'], [])
            with self.assertRaisesRegex(ValueError, 'one string.*list.*bar'):
                validate_proposal(proposal(options={'data_plot': ['bar', 'scatter']}), args, set())

    def test_individually_valid_settings_share_the_canonical_combination_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'supplied.csv'
            data.write_text('step,value\n0,1\n1,2\n')
            args = self.args(root, '--data-file', str(data))
            with self.assertRaisesRegex(ValueError, 'line/scatter require'):
                validate_proposal(proposal(options={'data_plot': 'line', 'value_column': ['value'],
                    'observation_unit': 'one coordinate', 'x_column': 'step', 'data_mode': 'observations'}), args, set())

    def test_code_repair_discovers_confirmed_validation_and_reuses_canonical_configuration(self):
        import shlex
        import tomllib
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            command = 'python -m unittest discover -s "tests with spaces"'
            (root / 'README.md').write_text('Testing: ' + command + '\n')
            (root / 'model.py').write_text('def compute():\n    return 1\n')
            args = self.args(root, '--kind', 'bug_fix', '--project', str(root), '--allow', 'model.py')
            args._reuse_protected = ['data/**']
            args._reuse_edit_policy = {'budget_profile': 'large', 'allow_large_edits': True}
            execution = {'argv': shlex.split(command), 'basis': [{'path': 'README.md', 'quote': command}]}
            client = Client(proposal('bug_fix', execution_proposal=execution))
            with patch('subprocess.run', side_effect=AssertionError('setup must not execute')):
                resolved = self.converse(args, client, ['y'])
                # Saved feedback setup retains original project permissions and
                # editing policy without another model call.
                resumed = self.args(root, '--resume-setup', str(resolved._start_root))
                resolved = self.converse(resumed, Client(), [])
                resolved.prepare_only = True
                with patch('sys.stdin.isatty', return_value=False), patch('simple_ar.cli.start.print_line'):
                    config = prepare_start(resolved)
            task_config = tomllib.loads((config.parent / 'code_task.toml').read_text())
            self.assertEqual(shlex.split(task_config['benchmark']['command']), execution['argv'])
            self.assertEqual(task_config['edit_scope']['allowed_patterns'], ['model.py'])
            self.assertIn('data/**', task_config['edit_scope']['protected_patterns'])
            self.assertEqual(task_config['execute']['budget_profile'], 'large')
            self.assertTrue(task_config['execute']['allow_large_edits'])
            self.assertEqual(task_config['environment']['mode'], 'current')
            self.assertIsNone(resolved.run_argv)
            self.assertEqual(resolved.goal, args.goal)
            facts = client.requests[0]['assets']
            for changed in ({**execution, 'basis': []}, {**execution, 'timeout_sec': 900},
                            {**execution, 'basis': [{'path': 'README.md', 'quote': 'python train.py'}]}):
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    validate_proposal(proposal('bug_fix', execution_proposal=changed), args, set(), facts=facts)
            args.validate = 'python different.py'
            with self.assertRaisesRegex(ValueError, 'explicit validate'):
                validate_proposal(proposal('bug_fix', execution_proposal=execution), args, {'validate'}, facts=facts)

    def test_reproduction_proposal_from_inspected_project_serializes_one_confirmed_command(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'README.md').write_text('First check inputs: python check.py\nCheck fixture mean: python run.py --data values.csv\n')
            (root / 'run.py').write_text("# Writes rows.json under SIMPLE_AR_OUTPUT_DIR\nif __name__ == '__main__':\n    print('METRIC mean=3.0')\n")
            paper = root / 'paper.md'
            paper.write_text('# Source\nA mean calculation for a constructed example, not a published paper.\n')
            args = self.args(root, '--kind', 'reproduction', '--project', str(root), '--document', str(paper),
                '--timeout-sec', '17', locked={'kind', 'project', 'document', 'timeout_sec'})
            execution = {'hypothesis': 'Fixture mean is three', 'dataset': 'Supplied values.csv; constructed example',
                'expected_outcome': 'Compare mean with three, not a paper reproduction score', 'metrics': ['mean'],
                'argv': ['python', 'run.py', '--data', 'values.csv'],
                'check_argv': ['python', 'check.py'],
                'output_files': {'raw': 'rows.json'},
                'basis': [{'path': 'README.md', 'quote': 'python run.py --data values.csv'},
                          {'path': 'README.md', 'quote': 'python check.py'}]}
            args.goal += f' Use existing Python {sys.executable}'
            facts = {'project_preparation': {'dependency_probe': {'python_requirement': {
                'declared': '>=999', 'matches_inspecting_python': False}}}}
            with self.assertRaisesRegex(ValueError, 'compatible human-named interpreter'):
                validate_proposal(proposal('reproduction', options={'environment': 'venv'},
                    execution_proposal=execution), args, set(), facts=facts)
            client = Client(proposal('reproduction', assets=[{'role': 'python', 'path_quote': sys.executable}]),
                            proposal('reproduction', execution_proposal=execution))
            with patch('subprocess.run', side_effect=AssertionError('setup must not execute')):
                resolved = self.converse(args, client, ['y', 'y'])
                resolved.prepare_only = True
                with patch('sys.stdin.isatty', return_value=False), patch('simple_ar.cli.start.print_line'):
                    config = prepare_start(resolved)
            values = research_defaults(['research-session', '--config', str(config)])
            self.assertEqual(values['command_argv'], [str(Path(sys.executable).absolute()), *execution['argv'][1:]])
            self.assertEqual(client.requests[1]['assets']['project_python'], str(Path(sys.executable).absolute()))
            contract = client.requests[1]['response_contract']
            self.assertEqual(contract['kind'], ['reproduction'])
            self.assertEqual(set(contract['options']), {'sources', 'environment', 'requirements', 'install_project'})
            self.assertNotIn('native_paired_analysis', contract)
            self.assertEqual(values['cwd'], str(root.resolve()))
            self.assertEqual(values['metric'], ['mean'])
            self.assertEqual(values['execution_details']['output_files'], {'raw': 'rows.json'})
            self.assertEqual(values['report_outline_strategy'], 'adaptive')
            self.assertEqual(values['timeout_sec'], 17)
            self.assertEqual(values['process_invocations'], 2)
            self.assertEqual(values['execution_details']['environment']['check_command'], [str(Path(sys.executable).absolute()), 'check.py'])
            self.assertEqual(values['process_wall_seconds'], 34)
            self.assertTrue((config.parent / 'preparation.md').is_file())
            self.assertFalse((config.parent / 'code_task.toml').exists())
            self.assertTrue(any(row['role'] == 'entry_source' for row in client.requests[0]['assets']['project_preparation']['excerpts']))
            preview = client.requests[0]['assets']['paper_previews'][0]
            self.assertIn('constructed example, not a published paper', preview['text'])
            self.assertEqual(preview['source_path'], str(paper.resolve()))
            import shlex
            (root / 'requirements.txt').write_text('# Fixture: no packages required\n')
            for filename, explicit, venv in (('adapter.py', False, False), ('adapters/export.py', True, False),
                                            ('venv_adapter.py', False, True)):
                with self.subTest(adapter=filename):
                    checker_text = 'python "check.py"'
                    flags = ['--allow', filename, '--validate', checker_text] if explicit else []
                    adapter_args = self.args(root, '--kind', 'reproduction', '--project', str(root),
                        '--document', str(paper), '--timeout-sec', '17', *flags,
                        locked={'kind', 'project', 'timeout_sec'} | ({'allow', 'validate'} if explicit else set()))
                    if venv:
                        adapter_args.project_python = Path(sys.executable)
                    adapter_execution = {**execution, 'argv': ['python', filename, '--data', 'values.csv'],
                        'code_preparation': {'allowed_paths': [filename], 'validation_argv': ['python', 'check.py']},
                        'basis': [*execution['basis'], {'path': 'run.py',
                            'quote': '# Writes rows.json under SIMPLE_AR_OUTPUT_DIR'}]}
                    adapter_execution.pop('check_argv')
                    adapter_client = Client(proposal('reproduction', execution_proposal=adapter_execution,
                        options={'environment': 'venv', 'requirements': ['requirements.txt']} if venv else {}))
                    with patch('subprocess.run', side_effect=AssertionError('setup must not execute')), \
                         patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='y'), \
                         patch('simple_ar.cli.intake_dialogue.print_line') as printed:
                        adopted = discuss_start(adapter_args, client=adapter_client)
                        display = '\n'.join(str(call.args[0]) for call in printed.call_args_list)
                        self.assertIn('NOT implemented or verified', display)
                        self.assertIn(filename, display)
                        self.assertIn('independent checker argv', display)
                        if venv:
                            self.assertIn('Separate environment authorization', display)
                        saved = read_json(adopted._start_root / 'setup.json')
                        self.assertEqual(saved['proposals'][-1]['execution_proposal']['code_preparation'],
                                         adapter_execution['code_preparation'])
                        no_replay = Client()
                        adopted = self.converse(self.args(root, '--resume-setup', str(adopted._start_root)), no_replay, [])
                        self.assertEqual(no_replay.requests, [])
                        adopted.prepare_only = True
                        with patch('simple_ar.cli.start.print_line'):
                            adapter_config = prepare_start(adopted)
                    self.assertEqual(adopted.allow, [filename])
                    self.assertEqual(shlex.split(adopted.validate), ['python', 'check.py'])
                    if explicit:
                        self.assertEqual(adopted.validate, checker_text)
                    adapter_values = research_defaults(['research-session', '--config', str(adapter_config)])
                    self.assertEqual(adapter_values['command_argv'], adapter_execution['argv'])
                    if venv:
                        self.assertEqual(adapter_values['execution_details']['environment']['requirements'], ['requirements.txt'])
                        self.assertEqual(adapter_values['execution_details']['environment']['python_executable'], sys.executable)
                        self.assertEqual(adapter_values['process_invocations'], 6)
                    else:
                        self.assertNotIn('environment', adapter_values['execution_details'])
                    self.assertEqual(adapter_values['execution_details']['initial_files'], [filename])
                    code = tomllib.loads(Path(adapter_values['code_task_config']).read_text())
                    self.assertEqual(code['edit_scope']['allowed_patterns'], [filename])
                    self.assertEqual(shlex.split(code['benchmark']['command']), ['python', 'check.py'])
                    self.assertEqual(code['execute']['timeout_sec'], 17)
                    self.assertEqual(code['environment']['mode'], 'current')
                    self.assertFalse((root / filename).exists())  # Proposed, not generated by intake.
                    self.assertEqual(code['execute']['budget_profile'], 'large')
                    self.assertTrue(code['execute']['allow_large_edits'])

    def test_reproduction_proposal_requires_actual_source_not_unread_or_model_authority(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            args = self.args(root, '--project', str(root))
            execution = {'hypothesis': 'Claim', 'dataset': 'Data', 'expected_outcome': 'Criterion', 'metrics': ['score'],
                'argv': ['python', 'run.py'], 'basis': [{'path': 'README.md', 'quote': 'python run.py'}]}
            facts = {'project_preparation': {'excerpts': [{'path': 'README.md', 'text': 'python run.py'}]}}
            validate_proposal(proposal('reproduction', execution_proposal=execution), args, set(), facts=facts)
            reference = {**execution, 'basis': [{'path': 'README.md'}]}
            selected_source = validate_proposal(proposal('reproduction', execution_proposal=reference),
                args, set(), facts=facts)['execution_proposal']
            self.assertEqual(selected_source['basis'], reference['basis'])
            for invalid in ([{'path': 'unread.py'}], [{'path': 42}], [{'path': 'README.md', 'quote': 'invented command'}]):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    validate_proposal(proposal('reproduction', execution_proposal={**execution, 'basis': invalid}),
                        args, set(), facts=facts)
            with self.assertRaises(ValueError):
                validate_proposal(proposal('reproduction', execution_proposal=reference), args, set(),
                    facts={'project_preparation': {'excerpts': [{'path': 'README.md', 'text': '  '}]}})
            for check in ([], 'python check.py', [42]):
                with self.subTest(check=check), self.assertRaisesRegex(ValueError, 'check_argv'):
                    validate_proposal(proposal('reproduction', execution_proposal={**execution,
                        'check_argv': check}), args, set(), facts=facts)
            args.check_argv = ['python', 'my_check.py']
            self.assertEqual(validate_proposal(proposal('reproduction', execution_proposal=execution),
                args, {'check_argv'}, facts=facts)['execution_proposal']['check_argv'], args.check_argv)
            with self.assertRaisesRegex(ValueError, 'explicit check_argv'):
                validate_proposal(proposal('reproduction', execution_proposal={**execution,
                    'check_argv': ['python', 'other.py']}), args, {'check_argv'}, facts=facts)
            args.run_argv = execution['argv']
            with self.assertRaisesRegex(ValueError, 'inspected source basis'):
                validate_proposal(proposal('reproduction', execution_proposal={**execution,
                    'check_argv': ['python', 'new.py'], 'basis': []}), args, set(), facts=facts)
            for attachments in ({'raw': '../outside.json'}, {'raw': '/outside.json'}, []):
                with self.subTest(attachments=attachments), self.assertRaises(ValueError):
                    validate_proposal(proposal('reproduction', execution_proposal={**execution,
                        'output_files': attachments}), args, set(), facts=facts)
            args.output_files = {'raw': 'rows.json'}
            args.metric_sources = {'score': {'output': 'raw', 'path': ['score']}}
            selected = validate_proposal(proposal('reproduction', execution_proposal=execution),
                args, {'metric_sources'}, facts=facts)['execution_proposal']
            self.assertEqual(selected['metric_sources'], args.metric_sources)
            self.assertEqual(validate_proposal(proposal('reproduction', execution_proposal={
                key: item for key, item in execution.items() if key != 'metrics'}), args, set(),
                facts=facts)['execution_proposal']['metrics'], ['score'])
            descriptions = {**execution, 'metrics': ['raw observations', 'per-method summary']}
            self.assertEqual(validate_proposal(proposal('reproduction', execution_proposal=descriptions),
                args, set(), facts=facts)['execution_proposal']['metrics'], ['score'])
            args.metric = ['score']
            with self.assertRaisesRegex(ValueError, 'explicit metric'):
                validate_proposal(proposal('reproduction', execution_proposal=descriptions),
                    args, {'metric'}, facts=facts)
            args.metric = None
            with self.assertRaisesRegex(ValueError, 'explicit metric_sources'):
                validate_proposal(proposal('reproduction', execution_proposal={**execution,
                    'metric_sources': {'score': {'output': 'raw', 'path': ['different']}}}),
                    args, {'metric_sources'}, facts=facts)
            with self.assertRaisesRegex(ValueError, 'explicit output_files'):
                validate_proposal(proposal('reproduction', execution_proposal={**execution,
                    'output_files': {'raw': 'other.json'}}), args, {'output_files'}, facts=facts)
            self.assertEqual(validate_proposal(proposal('reproduction', execution_proposal=execution),
                args, {'output_files'}, facts=facts)['execution_proposal']['output_files'], args.output_files)
            with self.assertRaisesRegex(ValueError, 'inspected source'):
                validate_proposal(proposal('reproduction', execution_proposal=execution), args, set(), facts={})
            with self.assertRaisesRegex(ValueError, 'timeout'):
                validate_proposal(proposal('reproduction', execution_proposal={**execution, 'timeout_sec': 99999}), args, set(), facts=facts)
            args.run_argv = ['python', 'different.py']
            with self.assertRaisesRegex(ValueError, 'explicit run_argv'):
                validate_proposal(proposal('reproduction', execution_proposal=execution), args, {'run_argv'}, facts=facts)
            with self.assertRaisesRegex(ValueError, 'only to reproduction'):
                validate_proposal(proposal('writing', execution_proposal=execution), args, set(), facts=facts)
            wrapped = {'project_preparation': {'excerpts': [{'path': 'README.md', 'text': 'python\n  run.py'}]}}
            resolved = validate_proposal(proposal('reproduction', execution_proposal=execution), args, set(), facts=wrapped)
            self.assertEqual(resolved['execution_proposal']['basis'][0]['quote'], 'python\n  run.py')
            with self.assertRaisesRegex(ValueError, 'does not match inspected source'):
                validate_proposal(proposal('reproduction', execution_proposal={**execution,
                    'basis': [{'path': 'README.md', 'quote': 'python not-run.py'}]}), args, set(), facts=wrapped)
            args.check_argv = None
            args.run_argv = None
            (root / 'run.py').write_text('def run(): return {"score": 1}\n')
            adapter_facts = {'project_preparation': {'excerpts': [
                {'path': 'README.md', 'role': 'project_instructions', 'text': 'python run.py'},
                {'path': 'run.py', 'role': 'entry_source', 'text': 'def run(): return {"score": 1}'}]}}
            adapter = {**execution, 'argv': ['python', 'adapter.py'],
                'basis': [*execution['basis'], {'path': 'run.py', 'quote': 'def run():'}],
                'code_preparation': {'allowed_paths': ['adapter.py'], 'validation_argv': ['python', 'verify.py']}}
            for path in ('../escape.py', '/absolute.py', 'C:/escape.py', '*.py', 'run.py',
                         'tests/check.py', 'data/adapter.py'):
                with self.subTest(scope=path), self.assertRaises(ValueError):
                    validate_proposal(proposal('reproduction', execution_proposal={**adapter,
                        'code_preparation': {**adapter['code_preparation'], 'allowed_paths': [path]}}),
                        args, set(), facts=adapter_facts)
            for changes in ({'validation_argv': ['python3', 'adapter.py']},
                            {'validation_argv': ['python', './adapter.py']}, {'validation_argv': []},
                            {'allowed_paths': []}, {'timeout_sec': 900}, {'python': '/other/python'}):
                with self.subTest(preparation=changes), self.assertRaises(ValueError):
                    validate_proposal(proposal('reproduction', execution_proposal={**adapter,
                        'code_preparation': {**adapter['code_preparation'], **changes}}), args, set(), facts=adapter_facts)
            with self.assertRaisesRegex(ValueError, 'check_argv'):
                validate_proposal(proposal('reproduction', execution_proposal={**adapter,
                    'check_argv': ['python', 'check.py']}), args, set(), facts=adapter_facts)
            accepted = validate_proposal(proposal('reproduction', options={'environment': 'venv'},
                execution_proposal=adapter), args, set(), facts=adapter_facts)
            self.assertEqual(accepted['options']['environment'], 'venv')
            # Source-defined invocation does not require a prose README role.
            source_only = {**adapter, 'basis': [{'path': 'run.py'}]}
            source_facts = {'project_preparation': {'excerpts': [adapter_facts['project_preparation']['excerpts'][1]]}}
            self.assertEqual(validate_proposal(proposal('reproduction', execution_proposal=source_only),
                args, set(), facts=source_facts)['execution_proposal']['basis'], source_only['basis'])
            with self.assertRaises(ValueError):
                validate_proposal(proposal('reproduction', options={'environment': 'venv', 'install_project': True},
                    execution_proposal=adapter), args, set(), facts=adapter_facts)
            with self.assertRaisesRegex(ValueError, 'inspected source'):
                validate_proposal(proposal('reproduction', execution_proposal=adapter), args, set(), facts=facts)
            args.allow = ['another.py']
            with self.assertRaisesRegex(ValueError, 'explicit allow'):
                validate_proposal(proposal('reproduction', execution_proposal=adapter), args, {'allow'}, facts=adapter_facts)
            args.validate = 'python another_check.py'
            with self.assertRaisesRegex(ValueError, 'explicit validate'):
                validate_proposal(proposal('reproduction', execution_proposal=adapter), args, {'validate'}, facts=adapter_facts)
            args.project = None
            args.cwd = root
            with self.assertRaisesRegex(ValueError, 'existing --project'):
                validate_proposal(proposal('reproduction', execution_proposal=adapter), args, set(), facts=adapter_facts)

    def test_reproduction_reads_indexed_configuration_before_asking_human_and_retains_reads_on_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'README.md').write_text('python run.py\n')
            (root / 'run.py').write_text("if __name__ == '__main__':\n    pass\n")
            (root / 'metrics.py').write_text("OUTPUT_NAME = 'coverage'\n")
            args = self.args(root, '--kind', 'reproduction', '--project', str(root))
            first = Client(proposal('reproduction', read_requests=[{'path': 'metrics.py'}]),
                           proposal('reproduction', questions=['Which published conclusion?']))
            self.assertIsNone(self.converse(args, first, ['stop']))
            self.assertEqual(len(first.requests), 2)
            self.assertTrue(any(row['path'] == 'metrics.py' and "'coverage'" in row['text']
                for row in first.requests[1]['assets']['project_preparation']['excerpts']))
            draft = self.draft(root)
            self.assertEqual(read_json(draft)['project_read_paths'], ['metrics.py'])
            resumed = self.args(root, '--resume-setup', str(draft.parent))
            client = Client(proposal('reproduction'))
            self.converse(resumed, client, ['Check the declared coverage, no new experiments.', 'y'])
            self.assertTrue(any(row['path'] == 'metrics.py' for row in client.requests[0]['assets']['project_preparation']['excerpts']))
            with self.assertRaisesRegex(ValueError, 'indexed'):
                validate_proposal(proposal('reproduction', read_requests=['../outside.py']), args, set(), facts={})

    def test_complete_file_lookup_is_idempotent_not_a_billable_json_correction_or_unbounded_loop(self):
        # Permissioned tool observations are not human clarification rounds.
        # A repository needing six source windows must still reach its proposal.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'README.md').write_text('Documented instruction\n')
            names = [f'config_{i}.toml' for i in range(6)]
            for name in names:
                (root / name).write_text('value = 1\n')
            args = self.args(root, '--kind', 'reproduction', '--project', str(root))
            client = Client(*(proposal('reproduction', read_requests=[name]) for name in names),
                            proposal('reproduction', questions=['Which conclusion should be checked?']))
            self.assertIsNone(self.converse(args, client, ['stop']))
            self.assertEqual(len(client.requests), 7)
            self.assertEqual(read_json(self.draft(root))['project_read_paths'], names)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'README.md').write_text('Documented instruction\n')
            args = self.args(root, '--kind', 'reproduction', '--project', str(root))
            client = Client(proposal('reproduction', read_requests=['README.md']), proposal('reproduction'))
            self.converse(args, client, ['y'])
            self.assertEqual(client.requests[1]['last_read_result']['already_supplied'], ['README.md'])
            self.assertEqual(read_json(self.draft(root)).get('project_read_paths'), [])
            self.assertFalse(read_json(self.draft(root)).get('rejected_proposals'))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'README.md').write_text('Documented instruction\n')
            args = self.args(root, '--kind', 'reproduction', '--project', str(root))
            client = Client(proposal('reproduction', read_requests=['README.md']), proposal('reproduction', read_requests=['README.md']))
            self.assertIsNone(self.converse(args, client, []))
            self.assertEqual(len(client.requests), 2)


if __name__ == "__main__":
    unittest.main()
