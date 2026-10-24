from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from simple_ar.report.export import ReportExportError, ReportExportResourceError, _compile, _copy_image, export_acm_report, _nodes


def _ast(image: str | None = None, citation: str = "p1") -> dict:
    blocks = [
        {"t": "Header", "c": [1, ["", [], []], [{"t": "Str", "c": "Evidence report"}]]},
        {"t": "Header", "c": [2, ["", [], []], [{"t": "Str", "c": "Abstract"}]]},
        {"t": "Para", "c": [{"t": "Str", "c": "A bounded finding."}]},
        {"t": "Header", "c": [2, ["", [], []], [{"t": "Str", "c": "Results"}]]},
        {"t": "Para", "c": [{"t": "Cite", "c": [[{"citationId": citation}], [{"t": "Str", "c": "result"}]]}]},
    ]
    if image:
        blocks.append({"t": "Para", "c": [{"t": "Image", "c": [["", [], []], [], [image, ""]]}]})
    return {"pandoc-api-version": [1, 22], "meta": {"header-includes": "untrusted"}, "blocks": blocks}


def _standalone_fixture(document: dict, output: Path) -> str:
    """Fake only the known built-in template slots; real Pandoc is server QA."""
    template = (output / 'template.latex').read_text(encoding='utf-8')
    metadata = document['meta']
    template = re.sub(r'\$if\(([^)]+)\)\$(.*?)\$endif\$',
        lambda match: match[2] if match[1] in metadata else '', template, flags=re.S)
    template = template.replace('$for(header-includes)$', '').replace('$endfor$', '')
    for key, value in metadata.items():
        if value['t'] == 'MetaList':
            text = '\n'.join(item['c'][0]['c'][1] for item in value['c'])
        else:
            text = value['c'] if value['t'] == 'MetaString' else ' '.join(
                node['c'] if node['t'] == 'Str' else node['c'][1]
                for node in _nodes(value['c']) if node.get('t') in {'Str', 'RawBlock', 'RawInline'})
        template = template.replace('$' + key + '$', text)
    return template.replace('$body$', document['blocks'][0]['c'][1])


class ReportExportTests(unittest.TestCase):
    def test_explicit_template_uses_bound_publication_owner_and_retains_failed_versions(self):
        from types import SimpleNamespace
        from simple_ar.core.budget import BudgetLedger, BudgetUnknownError
        from simple_ar.tools.workspace import WorkspaceToolGateway
        from simple_ar.tools.publication import ExportRequest, PublicationToolGateway, export_input_binding
        from simple_ar.app.tool_application import ToolApplication

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = BudgetLedger({'process_invocations': 30, 'process_wall_seconds': 600},
                storage_path=root / 'budget.json')
            workspace = WorkspaceToolGateway(root, {}, ledger)
            source = workspace.workspace / 'draft.md'
            original_markdown = '# Résumé α\n\nBody [@material-a1b2; @p2]. Narrative @material-a1b2; [-@p2].\n'
            source.write_text(original_markdown, encoding='utf-8')
            original_bib = ('@misc{material-a1b2, key={material-a1b2}, title={Evidence}, url={https://example.org/evidence}}\n'
                '@article{p2, author={Doe, Jane}, year={2024}, title={Authored evidence}, doi={10.1234/example}}\n')
            (source.parent / 'references.bib').write_text(original_bib, encoding='utf-8')
            bundle = source.parent / 'layout'
            bundle.mkdir()
            template = bundle / 'article.latex'
            template.write_text('\\documentclass{article}\n'
                '\\usepackage[authoryear,round]{natbib}\n'
                '$for(header-includes)$\n$header-includes$\n$endfor$\n'
                '\\title{$title$}\n\\begin{document}\n\\maketitle\n$abstract$\n$body$\n'
                '\\bibliographystyle{local}\n\\bibliography{references}\n\\end{document}\n', encoding='utf-8')
            companion = bundle / 'local.bst'
            companion.write_text('% trusted bibliography style fixture', encoding='utf-8')
            arguments = {'markdown': 'workspace/draft.md', 'compile_pdf': False,
                'template': {'path': 'layout/article.latex', 'assets': ['local.bst'], 'engine': 'xelatex'}}
            request = ExportRequest.model_validate(arguments)
            approved = export_input_binding(source, request)
            self.assertIn('layout/article.latex', approved['inputs'])
            self.assertIn('layout/local.bst', approved['inputs'])
            self.assertNotIn('template', export_input_binding(source, ExportRequest(markdown=arguments['markdown'])))
            self.assertNotIn('checks', ExportRequest(markdown=arguments['markdown']).model_dump())
            self.assertNotIn('checks', approved)
            historical_request = {'markdown': arguments['markdown'], 'title': '', 'compile_pdf': False,
                'template': arguments['template']}
            self.assertEqual(approved, {**historical_request, 'inputs': approved['inputs'], 'image_binding_version': 1})
            self.assertNotIn('citation_style', request.model_dump())
            self.assertEqual(ExportRequest.model_validate({**arguments, 'citation_style': None}).model_dump(),
                request.model_dump())
            schema = ExportRequest.model_json_schema()
            self.assertIn({'enum': ['numeric', 'template'], 'type': 'string'},
                schema['properties']['citation_style']['anyOf'])
            # Exercise the actual native approval consumer, using the old
            # serialized input shape as the expected pre-feature hash.
            app = SimpleNamespace(workspace=workspace, gateway=SimpleNamespace(session_owner=lambda name: None))
            call = {'function': {'name': 'export_report', 'arguments': json.dumps(arguments)}}
            old_token = hashlib.sha256(json.dumps({'call': call, 'inputs': approved}, sort_keys=True).encode()).hexdigest()
            self.assertEqual(ToolApplication.approval_token(app, call), old_token)
            gateway = PublicationToolGateway(root, workspace, ledger)
            standalone = []
            failure = False
            check_mode = 'pass'

            def render(spec, **kwargs):
                self.assertIs(kwargs['budget_ledger'], ledger)
                self.assertNotIn('OPENAI_API_KEY', spec.env)
                spec.output_dir.mkdir(parents=True, exist_ok=True)
                if spec.argv[0] in {'xelatex', 'bibtex'}:
                    (spec.cwd / 'main.pdf').write_bytes(b'%PDF-check-fixture')
                    text = 'Compiler fixture'
                elif spec.argv[0] == 'pdfinfo':
                    self.assertEqual(spec.env['LC_ALL'], 'C')
                    if check_mode == 'budget':
                        raise BudgetUnknownError('Retained unknown process usage')
                    text = 'Pages: 13\n' if check_mode == 'long' else 'Pages: 2\n'
                elif spec.argv[0] == 'pdftotext':
                    self.assertIn('UTF-8', spec.argv)
                    text = 'Visible résumé 张三' if check_mode == 'hit' else 'Visible résumé only'
                elif '--to=json' in spec.argv:
                    document = _ast(citation='material-a1b2')
                    document['blocks'].append({'t': 'Para', 'c': [
                        {'t': 'Cite', 'c': [[{'citationId': 'material-a1b2', 'citationMode': {'t': 'AuthorInText'}}], []]},
                        {'t': 'Cite', 'c': [[{'citationId': 'p2', 'citationMode': {'t': 'SuppressAuthor'}}], []]}]})
                    text = json.dumps(document)
                else:
                    document = json.loads(Path(spec.argv[-1]).read_text(encoding='utf-8'))
                    if '--standalone' in spec.argv:
                        standalone.append(document)
                        self.assertIn('--template=template.latex', spec.argv)
                        text = _standalone_fixture(document, spec.cwd)
                    elif '--to=markdown' in spec.argv:
                        text = original_markdown
                    else:
                        self.assertNotIn('citation_style', document['meta'])
                        text = r'Converted résumé α \citep{material-a1b2,p2} \citet{material-a1b2} \citeyearpar{p2}'
                (spec.output_dir / 'stdout.log').write_text(text, encoding='utf-8')
                return SimpleNamespace(returncode=int(failure and '--standalone' in spec.argv), stop_reason=None,
                    record={'streams': {'stdout': {'log_bytes_discarded': 0}}})

            with patch('simple_ar.report.export.shutil.which', side_effect=lambda name: name), \
                    patch('simple_ar.tools.publication.run_process', side_effect=render) as processes:
                result = gateway.call('export_report', arguments, identity='template')
                self.assertEqual(result.status, 'ok', result.error)
                exported = workspace.workspace / 'publications/template'
                self.assertEqual((exported / 'template.latex').read_bytes(), template.read_bytes())
                self.assertEqual((exported / 'local.bst').read_bytes(), companion.read_bytes())
                self.assertIn('workspace/publications/template/local.bst', result.artifacts)
                self.assertEqual(result.data['manifest']['format'], 'pandoc-latex-template')
                self.assertEqual(result.data['manifest']['compliance']['anonymity'], 'unverified')
                header = standalone[0]['meta']['header-includes']
                self.assertEqual(header['t'], 'MetaList')
                renderer_preamble = header['c'][0]['c'][0]['c'][1]
                self.assertIn('\\usepackage{fvextra,seqsplit,etoolbox}', renderer_preamble)
                self.assertNotIn('untrusted', renderer_preamble)
                self.assertNotIn('DeclareUnicodeCharacter', renderer_preamble)
                self.assertNotIn('citation_style', result.data['manifest'])
                self.assertNotIn('NAT@sort', renderer_preamble)
                self.assertEqual(standalone[0]['meta']['biblio-style']['c'], 'plainnat')
                count = processes.call_count
                self.assertEqual(gateway.call('export_report', arguments, identity='template').data, result.data)
                for style in ('numeric', 'template'):
                    styled = {**arguments, 'citation_style': style}
                    styled_request = ExportRequest.model_validate(styled)
                    self.assertEqual(export_input_binding(source, styled_request), {**approved, 'citation_style': style})
                    styled_call = {'function': {'name': 'export_report', 'arguments': json.dumps(styled)}}
                    self.assertNotEqual(ToolApplication.approval_token(app, styled_call), old_token)
                    self.assertIn('inputs changed', gateway.call('export_report', styled, identity='template').error)
                    self.assertEqual(processes.call_count, count)
                    identity = 'style-' + style
                    styled_result = gateway.call('export_report', styled, identity=identity)
                    self.assertEqual(styled_result.status, 'ok', styled_result.error)
                    project = workspace.workspace / 'publications' / identity
                    main = (project / 'main.tex').read_text(encoding='utf-8')
                    self.assertEqual(styled_result.data['manifest']['citation_style'], style)
                    saved_path = root / 'tools/publications' / identity / 'request.json'
                    self.assertEqual(json.loads(saved_path.read_text())['citation_style'], style)
                    self.assertIn('\\documentclass{article}', main)
                    self.assertIn('\\bibliographystyle{local}', main)
                    self.assertIn('\\usepackage[authoryear,round]{natbib}', main)
                    self.assertNotIn('\\usepackage[numbers', main)
                    self.assertEqual(main.count('\\usepackage[authoryear,round]{natbib}'), 1)
                    if style == 'numeric':
                        self.assertIn('\\AtEndPreamble{\\@ifpackageloaded{natbib}', main)
                        self.assertIn('\\setcitestyle{numbers,square,comma}', main)
                        self.assertIn('\\let\\citet\\citep\\let\\citeyearpar\\citep', main)
                    else:
                        self.assertNotIn('NAT@sort', main)
                    self.assertIn(r'\citep{material-a1b2,p2}', (project / 'body.tex').read_text(encoding='utf-8'))
                    self.assertEqual((project / 'references.bib').read_text(encoding='utf-8'), original_bib)
                    self.assertEqual((project / 'source.md').read_text(encoding='utf-8'), original_markdown)
                    count = processes.call_count
                    frozen = {path: path.read_bytes() for path in (saved_path, project / 'export.json', project / 'main.tex')}
                    restored = PublicationToolGateway(root, workspace, ledger)
                    self.assertEqual(restored.call('export_report', styled, identity=identity).data, styled_result.data)
                    for changed_style in (None, 'template' if style == 'numeric' else 'numeric'):
                        self.assertIn('inputs changed', restored.call('export_report',
                            {**arguments, 'citation_style': changed_style}, identity=identity).error)
                    self.assertEqual(processes.call_count, count)
                    self.assertEqual({path: path.read_bytes() for path in frozen}, frozen)
                for path in (template, companion):
                    original = path.read_bytes()
                    path.write_bytes(original + b'\n% changed')
                    self.assertNotEqual(export_input_binding(source, request), approved)
                    changed = gateway.call('export_report', arguments, identity='template')
                    self.assertEqual(changed.status, 'error')
                    self.assertIn('inputs changed', changed.error)
                    path.write_bytes(original)
                self.assertEqual(processes.call_count, count)
                (exported / 'local.bst').write_text('% changed after export', encoding='utf-8')
                self.assertIn('project files changed', gateway.call('export_report', arguments, identity='template').error)
                failure = True
                failed_arguments = {**arguments, 'citation_style': 'numeric'}
                failed = gateway.call('export_report', failed_arguments, identity='failed-template')
                self.assertEqual(failed.status, 'error')
                self.assertEqual(failed.data['manifest']['status'], 'failed')
                self.assertEqual(failed.data['manifest']['citation_style'], 'numeric')
                count = processes.call_count
                failed_manifest = (workspace.workspace / 'publications/failed-template/export.json').read_bytes()
                self.assertEqual(PublicationToolGateway(root, workspace, ledger).call(
                    'export_report', failed_arguments, identity='failed-template').data, failed.data)
                self.assertEqual((workspace.workspace / 'publications/failed-template/export.json').read_bytes(), failed_manifest)
                self.assertEqual(processes.call_count, count)
                self.assertTrue((root / 'tools/processes/failed-template-render-1/stdout.log').is_file())
                failure = False
                checked = {**arguments, 'compile_pdf': True,
                    'checks': {'max_pages': 4, 'forbidden_identifiers': ['张三', 'A.*literal']},
                    'delivery': {'summary': 'Finite user rules checked', 'artifacts': []}}
                checked_request = ExportRequest.model_validate(checked)
                self.assertEqual(export_input_binding(source, checked_request)['checks'], checked['checks'])
                for mode in ('pass', 'hit', 'long', 'missing'):
                    check_mode = mode
                    identity = 'checked-' + mode
                    with patch('simple_ar.report.export.shutil.which',
                            side_effect=lambda name: None if mode == 'missing' and name == 'pdftotext' else name):
                        outcome = gateway.call('export_report', checked, identity=identity)
                    self.assertEqual(outcome.status, 'ok' if mode == 'pass' else 'error', outcome.error)
                    self.assertEqual('delivery' in outcome.data, mode == 'pass')
                    receipt = outcome.data['manifest']['checks']
                    rows = receipt['results']
                    self.assertEqual(rows['max_pages']['status'], 'failed' if mode == 'long' else 'passed')
                    self.assertEqual(rows['forbidden_identifiers']['status'],
                        'failed' if mode == 'hit' else 'blocked' if mode == 'missing' else 'passed')
                    self.assertIn('pdf_sha256', receipt)
                    if mode == 'hit':
                        self.assertEqual(rows['forbidden_identifiers']['matches'], [{'literal': '张三', 'count': 1}])
                    self.assertEqual(outcome.data['output_integrity'], 'byte_bound')
                    count = processes.call_count
                    self.assertEqual(gateway.call('export_report', checked, identity=identity).data, outcome.data)
                    self.assertEqual(processes.call_count, count)
                    # A newly reconstructed owner reads the same failed/successful
                    # bytes, without rerunning Poppler or inventing past checks.
                    restored = PublicationToolGateway(root, workspace, ledger)
                    self.assertEqual(restored.call('export_report', checked, identity=identity).data, outcome.data)
                    self.assertEqual(processes.call_count, count)
                    project = workspace.workspace / 'publications' / identity
                    original_pdf = (project / 'main.pdf').read_bytes()
                    (project / 'main.pdf').write_bytes(original_pdf + b'changed')
                    self.assertIn('project files changed', restored.call('export_report', checked, identity=identity).error)
                    self.assertEqual(processes.call_count, count)
                check_mode = 'budget'
                count = processes.call_count
                stopped = gateway.call('export_report', {**checked, 'render_preview': True},
                    identity='checked-budget')
                self.assertEqual(stopped.status, 'error')
                self.assertNotIn('delivery', stopped.data)
                dispatched = [call.args[0].argv[0] for call in processes.call_args_list[count:]]
                self.assertEqual(dispatched[-1], 'pdfinfo')
                self.assertNotIn('pdftotext', dispatched)
                self.assertNotIn('pdftoppm', dispatched)
                self.assertEqual(stopped.data['manifest']['checks']['results']['max_pages']['status'], 'blocked')
                count = processes.call_count
                self.assertEqual(gateway.call('export_report', {**checked, 'render_preview': True},
                    identity='checked-budget').data, stopped.data)
                self.assertEqual(processes.call_count, count)
                self.assertNotIn('checks', result.data['manifest'])
                count = processes.call_count
                retroactive = gateway.call('export_report', checked, identity='template')
                self.assertIn('inputs changed', retroactive.error)
                self.assertEqual(processes.call_count, count)
            self.assertEqual(source.read_text(encoding='utf-8'), original_markdown)
            self.assertEqual((source.parent / 'references.bib').read_text(encoding='utf-8'), original_bib)

    def test_template_scope_partials_missing_companions_and_engine_fail_before_processes(self):
        from simple_ar.report.export import publication_template_inputs
        from simple_ar.tools.publication import ExportRequest, export_input_binding

        for style in ('author-year', '', True, 1, ['numeric']):
            with self.subTest(citation_style=style), self.assertRaises(ValueError):
                ExportRequest(markdown='workspace/draft.md', citation_style=style)
        with self.assertRaisesRegex(ReportExportError, 'citation_style must be'):
            export_acm_report(Path('unused'), Path('unused-output'), citation_style='author-year')

        for checks in ({}, {'max_pages': 0}, {'max_pages': True}, {'max_pages': 1.5},
                {'forbidden_identifiers': []}, {'forbidden_identifiers': [' ']},
                {'forbidden_identifiers': ['a' * 201]}, {'forbidden_identifiers': ['a'] * 33},
                {'forbidden_identifiers': ['same', 'same']}, {'forbidden_identifiers': ['line\nbreak']},
                {'official_policy': True}):
            with self.subTest(checks=checks), self.assertRaises(ValueError):
                ExportRequest(markdown='workspace/draft.md', checks=checks)
        with self.assertRaisesRegex(ValueError, 'checks requires compile_pdf=true'):
            ExportRequest(markdown='workspace/draft.md', compile_pdf=False, checks={'max_pages': 1})

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._source(root)
            template = source / 'layout.latex'
            valid = ('$for(header-includes)$\n$header-includes$\n$endfor$\n'
                '$if(title)$$title$$endif$\n\\begin{document}\n$body$')
            template.write_text(valid, encoding='utf-8')
            self.assertEqual(set(publication_template_inputs(source, {'path': template.name})), {'template.latex'})
            for options in ({'path': '../outside.tex'}, {'path': str(template.resolve())},
                    {'path': 'layout.latex', 'assets': ['missing.sty']},
                    {'path': 'layout.latex', 'assets': ['main.pdf']},
                    {'path': 'layout.latex', 'assets': ['run.py']},
                    {'path': 'layout.latex', 'engine': 'lualatex'},
                    {'path': 'layout.latex', 'top_level_division': 'automatic'}):
                with self.subTest(options=options), self.assertRaises(ReportExportError):
                    publication_template_inputs(source, options)
            for text in ('$body$', valid.replace('$for(header-includes)$', ''),
                    valid + '$other()$', valid + '${other()}', valid + '$other(\n)$'):
                template.write_text(text, encoding='utf-8')
                with self.subTest(text=text), self.assertRaises(ReportExportError):
                    publication_template_inputs(source, {'path': template.name})
            template.write_text(valid, encoding='utf-8')
            request = ExportRequest(markdown='workspace/report_body.md', template={'path': template.name})
            with self.assertRaisesRegex(ValueError, 'Derived publication'):
                export_input_binding(source / 'report_body.md', request, output_root=source)
            # A symlink cannot add a template from outside the manuscript scope.
            outside = root / 'outside.latex'
            outside.write_text(valid, encoding='utf-8')
            link = source / 'linked.latex'
            try:
                link.symlink_to(outside)
            except OSError:
                pass  # Windows may disallow symlink creation; server exercises it.
            else:
                with self.assertRaisesRegex(ReportExportError, 'out-of-scope'):
                    publication_template_inputs(source, {'path': link.name})

    def test_manifest_caption_binding_handles_both_pandoc_shapes_without_deleting_discussion(self):
        from copy import deepcopy
        from simple_ar.report.export import _bind_figure_captions
        caption = [{"t": "Str", "c": "Recorded"}, {"t": "Space"}, {"t": "Str", "c": "observations."}]
        image = {"t": "Image", "c": [["", [], []], [{"t": "Str", "c": "Generic"}], ["figures/raw.svg", ""]]}
        for modern in (False, True):
            for italic in (False, True):
                with self.subTest(modern=modern, italic=italic):
                    para = {"t": "Para", "c": [deepcopy(image)]}
                    figure = {"t": "Figure", "c": [["", [], []], [None, []], [para]]} if modern else para
                    duplicate = {"t": "Para", "c": [{"t": "Emph", "c": deepcopy(caption)}] if italic else deepcopy(caption)}
                    discussion = {"t": "Para", "c": [{"t": "Str", "c": "Interpretation remains limited."}]}
                    ast = {"blocks": [figure, duplicate, discussion]}
                    bound = _bind_figure_captions(ast, {"figures/raw.svg": caption})
                    self.assertEqual(bound, ["figures/raw.svg"])
                    self.assertEqual(ast["blocks"], [figure, discussion])
                    rendered_image = figure["c"][2][0]["c"][0] if modern else figure["c"][0]
                    self.assertEqual(rendered_image["c"][1], caption)
                    if modern:
                        self.assertEqual(figure["c"][1], [None, [{"t": "Plain", "c": caption}]])
                    else:
                        self.assertEqual(rendered_image["c"][-1][1], "fig:")
                    # Ordinary copied images have no manifest: only exact
                    # Pandoc-caption duplicates can be removed without guessing.
                    authored = deepcopy(image)
                    authored_alt = [{'t': 'Str', 'c': 'Accessibility description'}] if modern else caption
                    authored['c'][1] = deepcopy(authored_alt)
                    authored['c'][-1][1] = 'fig:'
                    para = {'t': 'Para', 'c': [authored]}
                    figure = {'t': 'Figure', 'c': [['', [], []],
                        [None, [{'t': 'Plain', 'c': deepcopy(caption)}]], [para]]} if modern else para
                    inner = {'blocks': [figure, deepcopy(duplicate), deepcopy(discussion)]}
                    self.assertEqual(_bind_figure_captions(inner, {}), [])
                    self.assertEqual(inner['blocks'], [figure, discussion])
                    self.assertEqual(authored['c'][1], authored_alt)
                    # Containers preserve the same ownership and do not bind
                    # modern Figure content a second time.
                    stale = [{'t': 'Str', 'c': 'Superseded manifest caption.'}]
                    for owner in ('manifest', 'markdown'):
                        for container in ('Div', 'BlockQuote', 'BulletList'):
                            children = [deepcopy(figure), deepcopy(duplicate), deepcopy(discussion)]
                            content = ([['topic', [], []], children] if container == 'Div' else
                                [children] if container == 'BulletList' else children)
                            wrapped = {'t': container, 'c': content}
                            nested = {'blocks': [wrapped]}
                            self.assertEqual(_bind_figure_captions(nested, {'figures/raw.svg': stale},
                                caption_owner=owner), ['figures/raw.svg'] if owner == 'manifest' else [])
                            # Deduplicate only the selected owner's caption; old
                            # managed exports must not delete different prose.
                            self.assertEqual(len(children), 3 if owner == 'manifest' else 2)
                            self.assertEqual(children[-1], discussion)
                            selected = children[0]
                            selected_image = selected['c'][2][0]['c'][0] if modern else selected['c'][0]
                            self.assertEqual(selected_image['c'][1],
                                stale if owner == 'manifest' else authored_alt)
                            if modern:
                                self.assertEqual(selected['c'][1][1][0]['c'],
                                    stale if owner == 'manifest' else caption)

    def test_caption_binding_leaves_unregistered_and_inline_images_unchanged(self):
        from copy import deepcopy
        from simple_ar.report.export import _bind_figure_captions
        ast = _ast("unknown.png")
        ast["blocks"].append({"t": "Para", "c": [{"t": "Str", "c": "Inline"},
            {"t": "Image", "c": [["", [], []], [], ["registered.png", ""]]}]})
        before = deepcopy(ast)
        self.assertEqual(_bind_figure_captions(ast, {"registered.png": [{"t": "Str", "c": "Caption"}]}), [])
        self.assertEqual(ast, before)
        # Similar text, numbered prose, and intervening blocks cannot prove
        # that a paragraph belongs to the image. Do not discard any of them.
        image = {'t': 'Image', 'c': [['', [], []], [{'t': 'Str', 'c': 'Observation'}],
            ['unknown.png', 'fig:']]}
        for following in (
                {'t': 'Para', 'c': [{'t': 'Str', 'c': 'Figure 7. Observation'}]},
                {'t': 'Para', 'c': [{'t': 'Strong', 'c': image['c'][1]}]},
                {'t': 'Header', 'c': [3, ['', [], []], image['c'][1]]}):
            with self.subTest(following=following['t']):
                ast = {'blocks': [{'t': 'Para', 'c': [deepcopy(image)]}, deepcopy(following),
                    {'t': 'Para', 'c': deepcopy(image['c'][1])}]}
                before = deepcopy(ast)
                self.assertEqual(_bind_figure_captions(ast, {}), [])
                self.assertEqual(ast, before)
        # Alt text alone is not a rendered caption when Pandoc has not made
        # an implicit figure, or a modern Figure has an empty caption.
        for modern in (False, True):
            uncaptioned = deepcopy(image)
            uncaptioned['c'][-1][1] = ''
            para = {'t': 'Para', 'c': [uncaptioned]}
            figure = {'t': 'Figure', 'c': [['', [], []], [None, []], [para]]} if modern else para
            ast = {'blocks': [figure, {'t': 'Para', 'c': deepcopy(image['c'][1])}]}
            before = deepcopy(ast)
            self.assertEqual(_bind_figure_captions(ast, {}), [])
            self.assertEqual(ast, before)

            # Non-caption alt text is not an author caption. Manifest fallback
            # is still required for both legacy images and modern Figures.
            fallback = [{'t': 'Str', 'c': 'Recorded fallback.'}]
            ast = {'blocks': [deepcopy(figure), {'t': 'Para', 'c': deepcopy(fallback)}]}
            self.assertEqual(_bind_figure_captions(ast, {'unknown.png': fallback},
                caption_owner='markdown'), ['unknown.png'])
            self.assertEqual(len(ast['blocks']), 1)
            selected = ast['blocks'][0]
            if modern:
                self.assertEqual(selected['c'][1], [None, [{'t': 'Plain', 'c': fallback}]])
            else:
                self.assertEqual(selected['c'][0]['c'][1], fallback)
                self.assertEqual(selected['c'][0]['c'][-1][1], 'fig:')

        # Short-only and multi-block modern captions are authored too, but do
        # not authorize single-paragraph deduplication or rewriting alt text.
        for caption in (
                [[{'t': 'Str', 'c': 'Short caption'}], []],
                [None, [{'t': 'Plain', 'c': image['c'][1]},
                    {'t': 'Para', 'c': [{'t': 'Str', 'c': 'Second paragraph'}]}]]):
            captioned_image = deepcopy(image)
            captioned_image['c'][1] = []
            ast = {'blocks': [{'t': 'Figure', 'c': [['', [], []], deepcopy(caption),
                [{'t': 'Para', 'c': [captioned_image]}]]},
                {'t': 'Para', 'c': deepcopy(image['c'][1])}]}
            before = deepcopy(ast)
            self.assertEqual(_bind_figure_captions(ast, {'unknown.png': fallback},
                caption_owner='markdown'), [])
            self.assertEqual(ast, before)

    def test_scientific_unicode_support_is_fixed_and_only_for_present_symbols(self):
        from simple_ar.report.export import _scientific_unicode_preamble

        preamble = _scientific_unicode_preamble("α ≤ β, Γ ≠ ∞; µm; H₀ x²; x⁻² y₊₁ z⁽⁺³⁾", r"\input{secret}")
        self.assertIn(r'\DeclareUnicodeCharacter{207B}{\ensuremath{{}^{-}}}', preamble)
        self.assertIn(r'\DeclareUnicodeCharacter{208A}{\ensuremath{{}_{+}}}', preamble)
        for code in ("03B1", "03B2", "0393", "2264", "2260", "221E", "00B5", "2080", "00B2"):
            self.assertIn("\\DeclareUnicodeCharacter{" + code + "}", preamble)
        self.assertNotIn("03B3", preamble)
        self.assertIn(r"\ensuremath{\alpha}", preamble)
        self.assertNotIn(r"\input", preamble)
        self.assertEqual(_scientific_unicode_preamble("Plain ASCII and unknown 中文"), "")

    def test_breakable_code_escapes_commands_and_keeps_separators(self):
        from simple_ar.report.export import _breakable_code
        rendered = _breakable_code(r"weighted_mc_error\input{secret}%")
        self.assertIn(r"\texttt{\seqsplit{", rendered)
        self.assertIn(r"{\_}", rendered)
        self.assertIn(r"{\textbackslash{}}input{\{}secret{\}}{\%}", rendered)
        self.assertNotIn(r"\input{", rendered)
        self.assertEqual(_breakable_code('AZ09' * 40), r'\texttt{\seqsplit{' + 'AZ09' * 40 + '}}')
        self.assertIn('{δ}', _breakable_code('δ_parameter'))
        self.assertEqual(_breakable_code('cmd  "x y"'), r'\texttt{\seqsplit{cmd{\ }{\ }"x{\ }y"}}')

    def test_technical_text_wraps_without_rewriting_source_math_or_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._source(root)
            document = _ast()
            tables = []
            for widths in ([None] * 3, [None] * 5, [0.3, 0.7], [None, 0.6]):
                columns = [[{'t': ('AlignLeft', 'AlignCenter', 'AlignRight')[index % 3]},
                    {'t': 'ColWidthDefault'} if width is None else {'t': 'ColWidth', 'c': width}]
                    for index, width in enumerate(widths)]
                # Modern Pandoc Table: attributes, caption, ColSpecs, head,
                # bodies, foot. Real parser/writer coverage lives alongside
                # the two supplied-layout compile/preview integration cases.
                tables.append({'t': 'Table', 'c': [['', [], []], [None, []], columns,
                    [['', [], []], []], [], [['', [], []], []]]})
            # Independent legacy AST shapes, as emitted by installed Pandoc
            # 2.9; the real integration below uses one actual parser version.
            for widths in ([0, 0, 0], [0.3, 0.7], [0, 0.6]):
                tables.append({'t': 'Table', 'c': [[], [{'t': 'AlignRight'}] * len(widths),
                    widths, [], []]})
            cell = [{'t': 'Str', 'c': 'latency+throughput'}, {'t': 'Space'},
                    {'t': 'Str', 'c': '0.123456789'}, {'t': 'Space'},
                    {'t': 'Math', 'c': [{'t': 'InlineMath'}, 'x/y']},
                    {'t': 'Link', 'c': [['', [], []], [{'t': 'Str', 'c': 'Reference'}],
                        ['https://example.test/unbroken-target', '']]}]
            tables[0]['c'][3][1] = [[['', [], []], [
                [['', [], []], {'t': 'AlignLeft'}, 1, 1, [{'t': 'Plain', 'c': cell}]]]]]
            tables[4]['c'][3] = [[{'t': 'Plain', 'c': cell}]]
            document['blocks'].extend(tables)
            document["blocks"].append({"t": "Para", "c": [
                {"t": "Str", "c": "train/validation/calibration/test"},
                {"t": "Space"}, {"t": "Str", "c": "10,123/3,639/2,048/5,803"},
                {"t": "Space"}, {"t": "Str", "c": r"x/\input{secret}%"},
                {"t": "Math", "c": [{"t": "InlineMath"}, "a/b"]},
                {"t": "Link", "c": [["", [], []], [{"t": "Str", "c": "source"}],
                    ["https://example.test/a/b", ""]]},
            ]})
            identifier = 'Az73' * 40
            command = 'runner --input ' + 'dataset/segment/' * 14 + 'sample.csv\n'
            malicious = r'\end{Verbatim}\input{secret} % $ & # _ ^ ~ {}'
            code = command + '\t' + malicious
            document['blocks'][-1]['c'].extend([
                {'t': 'Str', 'c': identifier},
                {'t': 'Code', 'c': [['', [], []], malicious]},
            ])
            document['blocks'].append({'t': 'CodeBlock', 'c': [['', ['sh'], []], code]})
            converted = []
            markdown = []
            def convert(argv, **kwargs):
                if "--to=json" in argv:
                    self.assertIn('--preserve-tabs', argv)
                    return json.dumps(document)
                data = json.loads(kwargs["text"])
                if "--to=latex" in argv:
                    converted.append(data)
                    if '--standalone' in argv:
                        return _standalone_fixture(data, kwargs['cwd'])
                else:
                    markdown.append(data)
                return "converted text"
            with patch("simple_ar.report.export.shutil.which", return_value="pandoc"), patch(
                    "simple_ar.report.export._run", side_effect=convert):
                export_acm_report(source, root / "acm")
            para = converted[0]["blocks"][-2]["c"]
            self.assertEqual(para[0], {"t": "RawInline", "c": ["latex",
                r"\seqsplit{train/validation/calibration/test}"]})
            self.assertEqual(para[2]["c"][1], r"\seqsplit{10,123/3,639/2,048/5,803}")
            self.assertNotIn(r"\texttt", para[0]["c"][1])
            self.assertNotIn(r"\input{", para[4]["c"][1])
            self.assertIn(r"{\textbackslash{}}input{\{}secret{\}}{\%}", para[4]["c"][1])
            self.assertEqual(para[5], document["blocks"][-2]["c"][5])
            self.assertEqual(para[6]["c"][-1][0], "https://example.test/a/b")
            self.assertEqual(para[7]['c'][1], r'\seqsplit{' + identifier + '}')
            self.assertNotIn(r'\input{', para[8]['c'][1])
            self.assertEqual(converted[0]['blocks'][-1]['c'], ['latex',
                r'\VerbatimInput[breaklines=true,breakanywhere=true,breaksymbolleft={},breaksymbolright={},breakanywheresymbolpre={},breakanywheresymbolpost={}]{code/block-1.txt}'])
            self.assertEqual((root / 'acm/code/block-1.txt').read_text(), code)
            self.assertEqual(markdown[0]['blocks'][-2:], document['blocks'][-2:])
            rendered_tables = [block for block in converted[0]['blocks'] if block['t'] == 'Table']
            for actual_cell in (rendered_tables[0]['c'][3][1][0][1][0][4][0]['c'],
                                rendered_tables[4]['c'][3][0][0]['c']):
                self.assertEqual(actual_cell[0], {'t': 'RawInline',
                    'c': ['latex', r'\seqsplit{latency+throughput}']})
                self.assertEqual(actual_cell[2:5], cell[2:5])  # Numbers and math are atomic.
                self.assertEqual(actual_cell[5], cell[5])  # Ordinary words and link targets stay authored.
            for index, fraction in ((0, 1 / 3), (1, 1 / 5)):
                self.assertEqual([column[1] for column in rendered_tables[index]['c'][2]],
                    [{'t': 'ColWidth', 'c': fraction}] * len(tables[index]['c'][2]))
                self.assertEqual([column[0] for column in rendered_tables[index]['c'][2]],
                    [column[0] for column in tables[index]['c'][2]])
            self.assertEqual(rendered_tables[2:4], tables[2:4])  # Explicit and mixed widths stay authored.
            self.assertEqual(rendered_tables[4]['c'][2], [1 / 3] * 3)
            self.assertEqual(rendered_tables[4]['c'][1], tables[4]['c'][1])
            self.assertEqual(rendered_tables[5:], tables[5:])
            self.assertEqual([block for block in markdown[0]['blocks'] if block['t'] == 'Table'], tables)
            self.assertIn(r'\usepackage{fvextra,seqsplit,etoolbox,xparse}', (root / 'acm/main.tex').read_text())
            self.assertIn("Missing authors/dates remain missing", (root / "acm/README.txt").read_text())

    def _source(self, root: Path) -> Path:
        report = root / "report"
        report.mkdir()
        (report / "report_body.md").write_text("# Evidence report\n\nBody [@p1].\n", encoding="utf-8")
        (report / "references.bib").write_text("@misc{p1, title={Evidence}}\n", encoding="utf-8")
        return report

    def test_supplied_templates_preserve_body_without_inferred_abstract_roles(self):
        # Template variables are presentation, not proof that authored text
        # will be rendered. Do not move a section out of the body by its name.
        for heading, abstract_slot, style, title_slot, base, expected_levels in (
                ('Abstract', False, None, False, 2, [1, 2, 2, 3]),
                ('Summary', True, None, True, 1, [1, 1, 2]),
                ('Executive summary', False, None, True, 3, [1, 1, 2]),
                ('Abstract', True, 'numeric', True, 2, [1, 1, 2]),
                ('Executive summary', False, 'template', False, 2, [1, 2, 2, 3])):
            with self.subTest(heading=heading, abstract_slot=abstract_slot, citation_style=style), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = self._source(root)
                document = _ast()
                document['blocks'][1]['c'][2] = [{'t': 'Str', 'c': heading}]
                for block in document['blocks'][1:]:
                    if block.get('t') == 'Header':
                        block['c'][0] = base
                document['blocks'].extend([
                    {'t': 'Header', 'c': [base + 1, ['', [], []], [{'t': 'Str', 'c': 'Summary'}]]},
                    {'t': 'Para', 'c': [{'t': 'Str', 'c': 'Nested discussion is still body content.'}]}])
                template = source / 'layout.latex'
                template.write_text('\\documentclass{article}\n'
                    '$for(header-includes)$\n$header-includes$\n$endfor$\n'
                    '\\usepackage[authoryear]{natbib}\n\\begin{document}\n'
                    + ('$title$\n' if title_slot else '')
                    + ('$if(abstract)$$abstract$$endif$\n' if abstract_slot else '')
                    + '$body$\n\\end{document}\n', encoding='utf-8')
                converted = []
                def convert(argv, **kwargs):
                    if '--to=json' in argv:
                        return json.dumps(document)
                    current = json.loads(kwargs['text'])
                    converted.append(current)
                    return (_standalone_fixture(current, kwargs['cwd']) if '--standalone' in argv
                            else json.dumps(current['blocks']))
                with patch('simple_ar.report.export.shutil.which', return_value='pandoc'), \
                        patch('simple_ar.report.export._run', side_effect=convert):
                    result = export_acm_report(source, root / 'export', template={'path': template.name},
                        **({'citation_style': style} if style is not None else {}))
                self.assertEqual(result['status'], 'exported')
                if style is None:
                    self.assertNotIn('citation_style', result)
                else:
                    self.assertEqual(result['citation_style'], style)
                main = (root / 'export/main.tex').read_text(encoding='utf-8')
                self.assertEqual('\\setcitestyle{numbers,square,comma}' in main, style == 'numeric')
                self.assertEqual(converted[-1]['meta']['biblio-style']['c'], 'plainurl' if style == 'numeric' else 'plainnat')
                self.assertEqual((root / 'export/template.latex').read_bytes(), template.read_bytes())
                headers = [node for node in _nodes(converted[0]['blocks']) if node.get('t') == 'Header']
                self.assertEqual([node['c'][0] for node in headers], expected_levels)
                self.assertEqual(headers[0]['c'][2], [{'t': 'Str', 'c': heading if title_slot else 'Evidence report'}])
                self.assertIn('A bounded finding.', (root / 'export/body.tex').read_text())
                self.assertIn('Nested discussion is still body content.', (root / 'export/body.tex').read_text())
                self.assertNotIn('abstract', converted[-1]['meta'])
                self.assertEqual(json.loads((root / 'export/source.md').read_text()), document['blocks'])

    def test_export_preserves_canonical_sources_and_separates_abstract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._source(root)
            calls: list[dict] = []
            def convert(argv, **kwargs):
                if "--to=json" in argv:
                    return json.dumps(_ast())
                document = json.loads(kwargs["text"])
                calls.append(document)
                if '--standalone' in argv:
                    return _standalone_fixture(document, kwargs['cwd'])
                return "converted text"
            with patch("simple_ar.report.export.shutil.which", return_value="pandoc"), patch("simple_ar.report.export._run", side_effect=convert):
                result = export_acm_report(source, root / "acm")
            self.assertEqual(result["status"], "exported")
            self.assertFalse(result["compiled"])
            self.assertFalse(result["quality_checked"])
            self.assertNotIn('pdf_preview', result)  # Default exports keep their historical shape.
            self.assertNotIn('template', result)
            self.assertNotIn('compliance', result)
            self.assertNotIn('checks', result)
            self.assertNotIn('citation_style', result)
            self.assertTrue(all(document["meta"] == {} for document in calls[:-1]))
            # Body LaTeX, portable Markdown, standalone LaTeX: title/abstract
            # metadata must not spend two additional process invocations.
            self.assertEqual(len(calls), 3)
            self.assertEqual(set(calls[-1]['meta']), {'title', 'abstract', 'header-includes', 'bibliography', 'biblio-style'})
            self.assertEqual(calls[-1]['meta']['title'], {'t': 'MetaInlines', 'c': _ast()['blocks'][0]['c'][2]})
            self.assertEqual(calls[-1]['meta']['abstract'], {'t': 'MetaBlocks', 'c': [_ast()['blocks'][2]]})
            body_headers = [block["c"][0] for block in calls[0]["blocks"] if block["t"] == "Header"]
            self.assertEqual(body_headers, [1])
            self.assertEqual((root / "acm/source.md").read_text(), "converted text")
            self.assertEqual((source / "report_body.md").read_text(), "# Evidence report\n\nBody [@p1].\n")
            self.assertEqual((root / "acm/references.bib").read_text(), (source / "references.bib").read_text())
            main = (root / "acm/main.tex").read_text()
            self.assertLess(main.index("\\begin{abstract}"), main.index("\\maketitle"))
            self.assertIn("\\authorsaddresses{}", main)
            self.assertIn('\\citestyle{acmnumeric}', main)
            self.assertNotIn('NAT@sort', main)
            self.assertIn("\\microtypesetup{expansion=false}", main)
            self.assertIn("\\RenewDocumentCommand{\\bibitem}{o m}", main)
            self.assertIn("\\saroriginalbibitem[{#1}]{#2}", main)
            self.assertNotIn('brace the full optional label', (root / "acm/README.txt").read_text())
            self.assertIn("full CM-Super", (root / "acm/README.txt").read_text())
            self.assertIn("\\Gin@nat@width>\\linewidth", main)
            self.assertNotIn('0.8\\textheight', main)
            self.assertIn('\\sarfigureavailable=\\@colht', main)
            self.assertIn('\\advance\\sarfigureavailable by -\\ht\\sarfigureafter', main)
            self.assertIn('Figure caption cannot fit on one page', main)
            self.assertIn("\\setkeys{Gin}{width=\\sarmaxwidth,height=\\sarmaxheight,keepaspectratio}", main)
            self.assertNotIn("\\setkeys{Gin}{width=\\linewidth,keepaspectratio}", main)
            self.assertTrue((root / "acm/export.json").is_file())
            with patch('simple_ar.report.export.shutil.which', return_value='pandoc'), \
                    patch('simple_ar.report.export._run', side_effect=convert):
                for style in ('template', 'numeric'):
                    explicit = root / ('acm-' + style)
                    receipt = export_acm_report(source, explicit, citation_style=style)
                    self.assertEqual(receipt['citation_style'], style)
                    explicit_main = (explicit / 'main.tex').read_text()
                    self.assertIn('\\citestyle{acmnumeric}', explicit_main)
                    if style == 'template':
                        self.assertEqual(explicit_main, main)
                    else:
                        self.assertIn('\\setcitestyle{numbers,square,comma}', explicit_main)
                    self.assertEqual((explicit / 'body.tex').read_bytes(), (root / 'acm/body.tex').read_bytes())
                    self.assertEqual((explicit / 'references.bib').read_bytes(), (source / 'references.bib').read_bytes())
            # The native consumer uses the same exporter with a shared process
            # owner; it does not require a special report filename or a model.
            from types import SimpleNamespace
            from simple_ar.core.budget import BudgetLedger
            from simple_ar.tools.workspace import DeliveryRequest, WorkspaceToolGateway
            from simple_ar.tools.publication import PublicationToolGateway, ExportRequest, export_input_binding
            native = root / "native"
            native.mkdir()
            ledger = BudgetLedger({"process_invocations": 10, "process_wall_seconds": 600},
                storage_path=native / "budget.json")
            workspace = WorkspaceToolGateway(native, {}, ledger)
            (workspace.workspace / "draft.md").write_text((source / "report_body.md").read_text())
            (workspace.workspace / "references.bib").write_text((source / "references.bib").read_text())
            figure = workspace.workspace / 'figures/main.png'
            figure.parent.mkdir()
            figure.write_bytes(b'original figure bytes')
            (workspace.workspace / 'draft.md').write_text((workspace.workspace / 'draft.md').read_text()
                + '\n![Existing diagram](figures/main.png)\n')
            private = workspace.workspace / '.env-hidden.png'
            private.write_bytes(b'private figure')
            gateway = PublicationToolGateway(native, workspace, ledger)
            display_image = 'figures/main.png'
            preview_failure = ''

            def render(spec, **kwargs):
                self.assertIs(kwargs["budget_ledger"], ledger)
                self.assertNotIn("OPENAI_API_KEY", spec.env)
                spec.output_dir.mkdir(parents=True, exist_ok=True)
                if spec.argv[0] == 'pdfinfo':
                    self.assertEqual(spec.env['LC_ALL'], 'C')
                    text = 'Pages: 2\n'
                elif spec.argv[0] == 'pdftoppm':
                    from PIL import Image
                    from simple_ar.core.budget import BudgetExceededError, BudgetUnknownError
                    if preview_failure in {'budget', 'unknown_budget'}:
                        error = BudgetUnknownError if preview_failure == 'unknown_budget' else BudgetExceededError
                        raise error('Retain the existing budget; no retry')
                    for page in (1, 2):
                        Image.new('RGB', (8, 12)).save(f'{spec.argv[-1]}-{page}.png')
                    text = ''
                else:
                    self.assertTrue(Path(spec.argv[-1]).is_file())
                    text = json.dumps(_ast(image=display_image)) if "--to=json" in spec.argv else "converted text"
                (spec.output_dir / "stdout.log").write_text(text)
                preview_process = spec.argv[0] == 'pdftoppm'
                return SimpleNamespace(returncode=1 if preview_process and preview_failure == 'nonzero' else 0,
                    stop_reason='timeout' if preview_process and preview_failure == 'timeout' else None,
                    record={"streams": {stream: {"log_bytes_discarded":
                        1 if preview_process and preview_failure == f'truncated_{stream}' else 0}
                        for stream in ('stdout', 'stderr')}})

            with patch("simple_ar.report.export.shutil.which", return_value="pandoc"), \
                    patch("simple_ar.tools.publication.run_process", side_effect=render) as processes:
                published = gateway.call("export_report", {"markdown": "workspace/draft.md", "compile_pdf": False}, identity="export")
                self.assertEqual(published.status, "ok", published.error)
                self.assertEqual(published.data['manifest']['figure_caption_owner'], 'markdown')
                count = processes.call_count
                self.assertGreater(count, 1)
                self.assertEqual(gateway.call("export_report", {"markdown": "workspace/draft.md", "compile_pdf": False}, identity="export").data,
                    published.data)
                self.assertEqual(processes.call_count, count)
                self.assertTrue(workspace.resolve(published.artifacts[0]).is_file())
                project_files = workspace.resolve_delivery(DeliveryRequest(
                    summary='Project files', artifacts=[published.data['project']]))['artifacts']
                self.assertEqual(published.artifacts, project_files)
                self.assertIn('workspace/publications/export/figures/figure-1.png', published.artifacts)
                self.assertIn('workspace/publications/export/README.txt', published.artifacts)
                self.assertEqual(published.data['artifact_sha256'], workspace.artifact_hashes(project_files))
                self.assertNotIn('delivery', published.data)
                delivery_request = {'markdown': 'workspace/draft.md', 'compile_pdf': False,
                    'delivery': {'summary': 'Editable export with original figure',
                        'artifacts': ['workspace/draft.md', 'workspace/figures'],
                        'unresolved': ['Content and visual quality not independently checked']}}
                delivered = gateway.call('export_report', delivery_request, identity='export-delivery')
                self.assertEqual(delivered.status, 'ok', delivered.error)
                self.assertTrue(set(delivered.artifacts).issubset(delivered.data['delivery']['artifacts']))
                self.assertIn('workspace/figures/main.png', delivered.data['delivery']['artifacts'])
                self.assertIn('workspace/publications/export-delivery/figures/figure-1.png',
                    delivered.data['delivery']['artifacts'])
                self.assertEqual(delivered.data['delivery']['unresolved'], delivery_request['delivery']['unresolved'])
                after_delivery = processes.call_count
                self.assertEqual(gateway.call('export_report', delivery_request, identity='export-delivery').data,
                    delivered.data)
                self.assertEqual(processes.call_count, after_delivery)
                bad_delivery = {**delivery_request, 'delivery': {'summary': 'Invalid',
                    'artifacts': ['workspace/.env-hidden.png']}}
                self.assertEqual(gateway.call('export_report', bad_delivery, identity='private-delivery').status, 'error')
                self.assertEqual(processes.call_count, after_delivery)
                with patch('simple_ar.tools.publication.export_acm_report', return_value={
                        'status': 'compile_failed', 'compiled': False, 'error': 'Renderer failure'}) as exporter:
                    failed_delivery = gateway.call('export_report', delivery_request, identity='failed-delivery')
                self.assertEqual(exporter.call_args.kwargs['caption_owner'], 'markdown')
                self.assertEqual(failed_delivery.status, 'error')
                self.assertNotIn('delivery', failed_delivery.data)
                count = after_delivery
                request = ExportRequest(markdown='workspace/draft.md', compile_pdf=False)
                self.assertNotIn('render_preview', request.model_dump(exclude_none=True))
                before = export_input_binding(workspace.workspace / 'draft.md', request,
                    output_root=workspace.workspace / 'publications')
                self.assertNotIn('render_preview', before)
                self.assertEqual(before, export_input_binding(workspace.workspace / 'draft.md',
                    request.model_copy(update={'render_preview': False}),
                    output_root=workspace.workspace / 'publications'))
                approved_preview = ExportRequest(markdown=request.markdown, render_preview=True)
                self.assertTrue(export_input_binding(workspace.workspace / 'draft.md', approved_preview,
                    output_root=workspace.workspace / 'publications')['render_preview'])
                self.assertIn('figures/main.png', before['inputs'])
                self.assertNotIn('.env-hidden.png', before['inputs'])
                self.assertFalse(any(key.startswith('publications/') for key in before['inputs']))
                figure.write_bytes(b'updated figure bytes')
                after = export_input_binding(workspace.workspace / 'draft.md', request,
                    output_root=workspace.workspace / 'publications')
                self.assertNotEqual(before['inputs']['figures/main.png'], after['inputs']['figures/main.png'])
                changed_figure = gateway.call('export_report', request.model_dump(), identity='export')
                self.assertEqual(changed_figure.status, 'error')
                self.assertEqual(processes.call_count, count)
                figure.write_bytes(b'original figure bytes')
                self.assertEqual(gateway.call('export_report', request.model_dump(), identity='export').data, published.data)
                self.assertEqual(processes.call_count, count)
                # Historical terminal versions restore only the coverage they
                # actually recorded, never a retroactively stronger approval.
                saved_request = native / 'tools/publications/export/request.json'
                historic = json.loads(saved_request.read_text())
                historic.pop('image_binding_version')
                historic['inputs'].pop('figures/main.png')
                saved_request.write_text(json.dumps(historic))
                self.assertEqual(gateway.call('export_report', request.model_dump(), identity='export').data, published.data)
                self.assertEqual(processes.call_count, count)
                (workspace.workspace / 'references.bib').write_text('@misc{changed, title={Changed bibliography}}')
                changed = gateway.call('export_report', {'markdown': 'workspace/draft.md', 'compile_pdf': False}, identity='export')
                self.assertEqual(changed.status, 'error')
                self.assertEqual(processes.call_count, count)
                (workspace.workspace / 'references.bib').write_text((source / 'references.bib').read_text())
                display_image = '.env-hidden.png'
                with patch('simple_ar.report.export._copy_image') as copy_image:
                    private_export = gateway.call('export_report', request.model_dump(), identity='private-export')
                    self.assertEqual(private_export.status, 'error')
                    copy_image.assert_not_called()

                # Reuse the native renderer fixture for complete compiled
                # projects and byte-bound recovery, rather than a fake exporter.
                display_image = 'figures/main.png'
                (figure.parent / 'vector.svg').write_text('<svg><text>Editable</text></svg>', encoding='utf-8')
                (figure.parent / 'figures_manifest.json').write_text(json.dumps({'figures': [
                    {'path': display_image, 'exports': {'svg': 'figures/vector.svg'}}]}), encoding='utf-8')
                bound_request = {**delivery_request, 'compile_pdf': True}

                def compile_project(output, has_citations, *, runner):
                    (output / 'main.pdf').write_bytes(b'original compiled PDF')
                    return {'status': 'compiled', 'compiled': True}

                with patch('simple_ar.report.export._compile', side_effect=compile_project):
                    bound = gateway.call('export_report', bound_request, identity='bound')
                self.assertEqual(bound.status, 'ok', bound.error)
                output = workspace.workspace / 'publications/bound'
                manifest = bound.data['manifest']
                vector = manifest['figure_exports'][display_image]['svg']
                self.assertTrue({f'workspace/publications/bound/{path}' for path in
                    ('main.pdf', 'README.txt', 'figures/figure-1.png', vector)}.issubset(bound.artifacts))
                self.assertTrue(set(bound.artifacts).issubset(bound.data['delivery']['artifacts']))
                after_bound = processes.call_count
                # Recreate the gateway to exercise durable receipts, not memory.
                gateway = PublicationToolGateway(native, workspace, ledger)
                for relative, mutation in [
                        *((path, 'modify') for path in ('main.pdf', 'main.tex', 'body.tex', 'source.md',
                            'references.bib', 'figures/figure-1.png', vector)),
                        ('export.json', 'failed_manifest'), ('figures/figure-1.png', 'delete'),
                        ('added.tex', 'add')]:
                    with self.subTest(file=relative, mutation=mutation):
                        path = output / relative
                        original = path.read_bytes() if path.exists() else None
                        if mutation == 'delete':
                            path.unlink()
                        else:
                            changed_bytes = (json.dumps({**manifest, 'status': 'failed'}).encode()
                                if mutation == 'failed_manifest' else (original or b'') + b'user change')
                            path.write_bytes(changed_bytes)
                        rejected = gateway.call('export_report', bound_request, identity='bound')
                        self.assertEqual(rejected.status, 'error')
                        self.assertIn('Publication project files changed or are missing', rejected.error)
                        self.assertNotIn('delivery', rejected.data)
                        self.assertEqual(path.read_bytes() if path.exists() else None,
                            None if mutation == 'delete' else changed_bytes)
                        if original is None:
                            path.unlink()
                        else:
                            path.write_bytes(original)
                self.assertEqual(gateway.call('export_report', bound_request, identity='bound').data, bound.data)
                self.assertEqual(processes.call_count, after_bound)

                # Previews use the same process fixture and durable whole-project
                # binding as PDF/TeX, including recovery after a new gateway.
                preview_request = {**bound_request, 'render_preview': True}
                with patch('simple_ar.report.export.shutil.which', side_effect=lambda name: name), patch(
                        'simple_ar.report.export._compile', side_effect=compile_project):
                    preview = gateway.call('export_report', preview_request, identity='preview')
                    self.assertEqual(preview.status, 'ok', preview.error)
                    self.assertEqual(preview.data['preview_images'], [
                        'workspace/publications/preview/previews/page-1.png',
                        'workspace/publications/preview/previews/page-2.png'])
                    self.assertTrue(set(preview.data['preview_images']).issubset(preview.artifacts))
                    self.assertTrue(set(preview.data['preview_images']).issubset(preview.data['delivery']['artifacts']))
                    self.assertFalse(preview.data['manifest']['quality_checked'])
                    self.assertFalse(preview.data['manifest']['pdf_preview']['visual_reviewed'])
                    gateway = PublicationToolGateway(native, workspace, ledger)
                    after_preview = processes.call_count
                    self.assertEqual(gateway.call('export_report', preview_request, identity='preview').data, preview.data)
                    self.assertEqual(processes.call_count, after_preview)
                    for mutation in ('modify', 'delete', 'add'):
                        path = workspace.resolve(preview.data['preview_images'][0])
                        if mutation == 'add':
                            path = path.with_name('extra.png')
                        original = path.read_bytes() if path.exists() else None
                        if mutation == 'delete':
                            path.unlink()
                        else:
                            path.write_bytes(b'changed preview')
                        rejected = gateway.call('export_report', preview_request, identity='preview')
                        self.assertEqual(rejected.status, 'error', rejected.error)
                        self.assertNotIn('delivery', rejected.data)
                        self.assertEqual(processes.call_count, after_preview)
                        if original is None:
                            path.unlink()
                        else:
                            path.write_bytes(original)
                    for preview_failure in ('nonzero', 'timeout', 'truncated_stdout', 'truncated_stderr',
                            'budget', 'unknown_budget'):
                        with self.subTest(preview_failure=preview_failure):
                            identity = f'preview-{preview_failure}'
                            failed = gateway.call('export_report', preview_request, identity=identity)
                            self.assertEqual(failed.status, 'error', failed.error)
                            self.assertTrue(failed.data['manifest']['compiled'])
                            self.assertFalse(failed.data['manifest']['pdf_preview']['rendered'])
                            self.assertFalse(failed.data['manifest']['quality_checked'])
                            self.assertNotIn('delivery', failed.data)
                            failed_count = processes.call_count
                            self.assertEqual(gateway.call('export_report', preview_request, identity=identity).data, failed.data)
                            self.assertEqual(processes.call_count, failed_count)
                    preview_failure = ''
                after_bound = processes.call_count
                owner = native / 'tools/publications/bound'
                (owner / 'result.json').unlink()
                interrupted = gateway.call('export_report', bound_request, identity='bound')
                self.assertEqual(interrupted.status, 'error')
                self.assertIn('no successful output byte binding', interrupted.error)
                self.assertFalse((owner / 'result.json').exists())
                # Legacy input-only receipts remain readable as history. They
                # never gain present-byte certification or an automatic delivery.
                saved = json.loads((owner / 'request.json').read_text())
                saved.pop('output_binding_version')
                input_only = {key: value for key, value in saved.items() if key != 'image_binding_version'}
                input_only['inputs'] = {key: value for key, value in saved['inputs'].items()
                    if key in {'draft.md', 'references.bib', 'figures/figures_manifest.json'}}
                markdown_only = {key: value for key, value in input_only.items() if key != 'inputs'}
                markdown_only['sha256'] = saved['inputs']['draft.md']
                # Older terminal manifests have no caption-owner field. Reading
                # them must neither migrate their bytes nor infer a new policy.
                manifest = {key: value for key, value in manifest.items() if key != 'figure_caption_owner'}
                historical_bytes = json.dumps(manifest).encode('utf-8')
                (output / 'export.json').write_bytes(historical_bytes)
                for historic in (saved, input_only, markdown_only):
                    with self.subTest(historic=historic):
                        (owner / 'request.json').write_text(json.dumps(historic), encoding='utf-8')
                        legacy = gateway.call('export_report', bound_request, identity='bound')
                        self.assertEqual(legacy.status, 'blocked', legacy.error)
                        self.assertEqual(legacy.data['historical_manifest'], manifest)
                        self.assertEqual((output / 'export.json').read_bytes(), historical_bytes)
                        self.assertEqual(legacy.data['output_integrity'], 'historical_unbound')
                        self.assertNotIn('manifest', legacy.data)
                        self.assertNotIn('delivery', legacy.data)
                        self.assertFalse((owner / 'result.json').exists())
                self.assertEqual(processes.call_count, after_bound)
                self.assertEqual(ledger.entries, ())

    def test_requested_pdf_preview_renders_all_pages_or_retains_failed_request(self):
        from PIL import Image

        cases = ('complete', 'twelve_pages', 'missing_poppler', 'too_many_pages', 'zero_pages',
            'missing_count', 'duplicate_count', 'renderer_failure', 'renderer_timeout',
            'partial_pages', 'unexpected_page', 'large_dimensions', 'large_file', 'invalid_png',
            'missing_tex', 'missing_pdf')
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = self._source(root)
                original = (source / 'report_body.md').read_bytes()
                output = root / 'acm'
                calls = []
                page_count = 12 if case == 'twelve_pages' else 13 if case == 'too_many_pages' else 0 if case == 'zero_pages' else 2

                def compile_project(directory, has_citations, *, runner):
                    if case == 'missing_tex':
                        return {'status': 'exported', 'compiled': False, 'compile_note': 'TeX unavailable'}
                    if case != 'missing_pdf':
                        (directory / 'main.pdf').write_bytes(b'compiled PDF fixture')
                    return {'status': 'compiled', 'compiled': True}

                def render(argv, **kwargs):
                    calls.append((argv, kwargs))
                    if argv[0] == 'pdfinfo':
                        self.assertEqual(kwargs['env'], {'LC_ALL': 'C'})
                        self.assertEqual(kwargs['timeout'], 30)
                        return ('No page count\n' if case == 'missing_count' else
                            'Pages: 2\nPages: 3\n' if case == 'duplicate_count' else f'Pages: {page_count}\n')
                    if argv[0] == 'pdftoppm':
                        self.assertEqual(argv[1:11], ['-png', '-r', '120', '-scale-to', '1600',
                            '-f', '1', '-l', str(page_count), str(output / 'main.pdf')])
                        self.assertEqual(kwargs['timeout'], 90)
                        if case == 'renderer_failure':
                            raise ReportExportError('Poppler rendering failed')
                        if case == 'renderer_timeout':
                            raise subprocess.TimeoutExpired(argv, 90)
                        for page in range(1, page_count + (2 if case == 'unexpected_page' else 1)):
                            if case == 'partial_pages' and page == page_count:
                                continue
                            path = Path(f'{argv[-1]}-{page:0{len(str(page_count))}d}.png')
                            if case == 'invalid_png':
                                path.write_bytes(b'not PNG')
                            elif case == 'large_file':
                                path.write_bytes(b'x' * (5 * 1024 * 1024 + 1))
                            else:
                                Image.new('RGB', (1601, 1) if case == 'large_dimensions' else (8, 12)).save(path)
                        return ''
                    return json.dumps(_ast()) if '--to=json' in argv else 'converted text'

                def which(name):
                    return None if case == 'missing_poppler' and name == 'pdftoppm' else name

                with patch('simple_ar.report.export.shutil.which', side_effect=which), patch(
                        'simple_ar.report.export._compile', side_effect=compile_project):
                    if case in {'complete', 'twelve_pages'}:
                        manifest = export_acm_report(source, output, compile_pdf=True, render_preview=True, runner=render)
                        preview = manifest['pdf_preview']
                        self.assertEqual(manifest['status'], 'compiled')
                        self.assertTrue(preview['rendered'])
                        self.assertEqual(preview['page_count'], page_count)
                        self.assertEqual(len(preview['pages']), page_count)
                        self.assertTrue(all((output / path).is_file() for path in preview['pages']))
                    else:
                        with self.assertRaises(ReportExportError):
                            export_acm_report(source, output, compile_pdf=True, render_preview=True, runner=render)
                        manifest = json.loads((output / 'export.json').read_text())
                        self.assertEqual(manifest['status'], 'failed')
                        self.assertFalse(manifest['pdf_preview']['rendered'])
                        self.assertEqual(manifest['pdf_preview']['pages'], [])
                        self.assertTrue(manifest['error'])
                    self.assertEqual(manifest['compiled'], case != 'missing_tex')
                    self.assertFalse(manifest['quality_checked'])
                    self.assertFalse(manifest['pdf_preview']['visual_reviewed'])
                    self.assertEqual({key: manifest['pdf_preview'][key] for key in
                        ('max_pages', 'dpi', 'max_dimension_px')},
                        {'max_pages': 12, 'dpi': 120, 'max_dimension_px': 1600})
                    self.assertEqual((source / 'report_body.md').read_bytes(), original)
                    self.assertTrue((output / 'main.tex').is_file())
                    if case in {'missing_poppler', 'too_many_pages', 'zero_pages', 'missing_count',
                            'duplicate_count', 'missing_tex', 'missing_pdf'}:
                        self.assertFalse(any(argv[0] == 'pdftoppm' for argv, _ in calls))
                        self.assertFalse((output / 'previews').exists())

    def test_preview_requires_compilation_before_exporting_or_running_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._source(root)
            with patch('simple_ar.report.export._run') as run:
                with self.assertRaisesRegex(ReportExportError, 'render_preview requires compile_pdf=true'):
                    export_acm_report(source, root / 'acm', render_preview=True)
                run.assert_not_called()
                self.assertFalse((root / 'acm').exists())
                with self.assertRaisesRegex(ReportExportError, 'checks requires compile_pdf=true'):
                    export_acm_report(source, root / 'acm', checks={'max_pages': 1})
                run.assert_not_called()

    def test_finite_checks_keep_independent_observations_when_text_is_unavailable(self):
        from copy import deepcopy
        from simple_ar.report.export import _check_publication_pdf

        with tempfile.TemporaryDirectory() as tmp:
            initial = {'status': 'blocked', 'rules': {'max_pages': 2, 'forbidden_identifiers': ['张三']},
                'results': {'max_pages': {'status': 'blocked'}, 'forbidden_identifiers': {'status': 'blocked'}}}
            # A few distinct diagnostic boundaries; no template/engine matrix.
            for mode in ('oversize', 'empty', 'truncated', 'malformed_count'):
                output = Path(tmp) / mode
                output.mkdir()
                (output / 'main.pdf').write_bytes(b'%PDF-observation-fixture')
                receipt = deepcopy(initial)
                def render(argv, **kwargs):
                    if argv[0] == 'pdfinfo':
                        return 'Pages: 2\nPages: 3\n' if mode == 'malformed_count' else 'Pages: 2\n'
                    if mode == 'truncated':
                        raise ReportExportError('Renderer failed or output truncated; inspect process receipt')
                    return 'x' * 1_000_001 if mode == 'oversize' else '' if mode == 'empty' else 'Unicode 张三'
                with self.subTest(mode=mode), patch('simple_ar.report.export.shutil.which', side_effect=lambda name: name):
                    _check_publication_pdf(output, receipt, runner=render)
                rows = receipt['results']
                self.assertEqual(receipt['status'], 'blocked')
                self.assertEqual(rows['max_pages']['status'], 'blocked' if mode == 'malformed_count' else 'passed')
                self.assertEqual(rows['forbidden_identifiers']['status'], 'failed' if mode == 'malformed_count' else 'blocked')
                self.assertIn('pdf_sha256', receipt)

    def test_exported_markdown_and_figures_move_together_without_external_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._source(root)
            (source / "original.png").write_bytes(b"image")
            vector = '<svg xmlns="http://www.w3.org/2000/svg"><text>Editable figure</text></svg>'
            (source / "original.svg").write_text(vector, encoding="utf-8")
            (source / "figures").mkdir()
            caption = [{'t': 'Str', 'c': 'Recorded comparison.'}]
            (source / "figures/figures_manifest.json").write_text(json.dumps({"figures": [
                {"path": "original.png", "caption": 'Recorded comparison.',
                    "exports": {"svg": "original.svg"}}]}), encoding="utf-8")
            document = _ast(image="original.png")
            document['blocks'][-1]['c'][0]['c'][1] = [{'t': 'Str', 'c': 'Generic image description'}]
            # Actual repeated prose is not an adjacent exact caption duplicate.
            document['blocks'][-1:-1] = [
                {'t': 'Para', 'c': [{'t': 'Str', 'c': 'Real duplicated discussion.'}]},
                {'t': 'Para', 'c': [{'t': 'Str', 'c': 'Real duplicated discussion.'}]},
                {'t': 'Para', 'c': [{'t': 'Code', 'c': [['', [], []], 'literal_identifier']},
                    {'t': 'Link', 'c': [['', [], []], [{'t': 'Str', 'c': 'Figure download'}], ['original.png', '']]}]},
            ]
            document['blocks'].append({'t': 'Para', 'c': [{'t': 'Emph', 'c': caption}]})
            document["blocks"].append({"t": "Para", "c": [{"t": "Link", "c": [
                ["", [], []], [{"t": "Str", "c": "Measured source"}], ["../../experiment/results.json", ""],
            ]}]})
            converted_documents = []
            def convert(argv, **kwargs):
                if "--to=json" in argv:
                    if kwargs['text'] in {'Recorded comparison.', 'Other caption.'}:
                        return json.dumps({'blocks': [{'t': 'Para', 'c':
                            [{'t': 'Str', 'c': kwargs['text']}]}]})
                    return json.dumps(document)
                data = json.loads(kwargs["text"])
                if '--standalone' not in argv:
                    converted_documents.append((tuple(argv), data))
                if "--to=markdown" in argv:
                    image = data["blocks"][-2]["c"][0]["c"][-1][0]
                    self.assertEqual(data['blocks'][-2]['c'][0]['c'][1], caption)
                    self.assertFalse(any(row.get('t') == 'Para' and row.get('c') ==
                        [{'t': 'Emph', 'c': caption}] for row in data['blocks']))
                    self.assertEqual(data["blocks"][-1]["c"][0]["t"], "Span")
                    prose = ' '.join(node['c'] for node in _nodes(data['blocks']) if node.get('t') == 'Str')
                    return f"{prose}\n![Measured figure]({image})\n"
                return "converted text"
            # Hash the original bytes, including CRLF, rather than normalized text.
            body_path = source / 'report_body.md'
            body_path.write_bytes(body_path.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))
            original_body = (source / 'report_body.md').read_bytes()
            original_inputs = {path: path.read_bytes() for path in (source / 'references.bib',
                source / 'original.png', source / 'original.svg', source / 'figures/figures_manifest.json')}
            def assert_view(result, output, owner):
                receipt = result['inspection_view']
                raw = (output / receipt['path']).read_bytes()
                view = json.loads(raw)
                self.assertEqual(receipt, {'path': 'publication_view.json',
                    'sha256': hashlib.sha256(raw).hexdigest(), 'schema': 'publication_view.v1'})
                self.assertEqual(view['schema_version'], receipt['schema'])
                self.assertEqual(view['input_body_sha256'], hashlib.sha256(original_body).hexdigest())
                self.assertEqual(view['caption_owner'], owner)
                self.assertEqual(view['logical_document'], converted_documents[-1][1])
                self.assertEqual(view['source_markdown_sha256'], hashlib.sha256((output / 'source.md').read_bytes()).hexdigest())
                nodes = list(_nodes(view['logical_document']))
                self.assertEqual(sum(node == {'t': 'Str', 'c': 'Real duplicated discussion.'} for node in nodes), 2)
                self.assertEqual(sum(node == caption[0] for node in nodes), 1)
                self.assertTrue(any(node.get('t') == 'Code' for node in nodes))
                self.assertFalse(any(node.get('t') in {'RawInline', 'RawBlock'} for node in nodes))
                mapping = view['image_mapping']['original.png']
                self.assertEqual(mapping['display'], 'figures/figure-1.png')
                self.assertEqual(set(mapping['files']), {'figures/figure-1.png', result['figure_exports']['original.png']['svg']})
                self.assertTrue(all((output / path).is_file() for path in mapping['files']))
                self.assertEqual([node['c'][-1][0] for node in nodes if node.get('t') == 'Image'], [mapping['display']])
                self.assertEqual([node['c'][-1][0] for node in nodes if node.get('t') == 'Link'], [mapping['display']])
                self.assertIn('Measured source', (output / 'source.md').read_text())
                self.assertIn('Real duplicated discussion.', (output / 'source.md').read_text())
                self.assertFalse(result['quality_checked'])
                self.assertIn('No pagination, visual or scientific certification', view['scope'])
                return view
            with patch("simple_ar.report.export.shutil.which", return_value="pandoc"), patch("simple_ar.report.export._run", side_effect=convert) as transport:
                result = export_acm_report(source, root / "acm")
                self.assertEqual(result['figure_caption_owner'], 'manifest')
                self.assertEqual(transport.call_count, 5)
                view = assert_view(result, root / 'acm', 'manifest')
                self.assertIsNone(view['title_intent']['explicit_title'])
                # A later canonical edit must not require editing the recorded
                # manifest. The same pipeline still exports its vector variant.
                caption = [{'t': 'Str', 'c': 'Revised canonical comparison.'}]
                document['blocks'][-3]['c'][0]['c'][1] = caption
                document['blocks'][-3]['c'][0]['c'][-1][1] = 'fig:'
                document['blocks'][-2]['c'] = [{'t': 'Emph', 'c': caption}]
                revised = export_acm_report(source, root / 'canonical', caption_owner='markdown', title='Actual publication title')
                self.assertEqual(revised['figure_caption_owner'], 'markdown')
                self.assertEqual(transport.call_count, 10)
                view = assert_view(revised, root / 'canonical', 'markdown')
                self.assertEqual(view['title_intent'], {'explicit_title': 'Actual publication title',
                    'publication_title': [{'t': 'Str', 'c': 'Actual'}, {'t': 'Space'},
                        {'t': 'Str', 'c': 'publication'}, {'t': 'Space'}, {'t': 'Str', 'c': 'title'}],
                    'template_renders_title': True})
                self.assertEqual(view['logical_document']['blocks'][0]['c'][2], [{'t': 'Str', 'c': 'Evidence report'}])
                self.assertEqual(revised['bound_figure_captions'], [])
                # Legacy Markdown ownership keeps the authored caption even
                # when the adjacent manifest still records the old wording.
                # Check both writer inputs, not only the Markdown fixture.
                revised_documents = converted_documents[-2:]
                self.assertEqual({arg for argv, _ in revised_documents for arg in argv
                    if arg.startswith('--to=')}, {'--to=latex', '--to=markdown'})
                for _, converted in revised_documents:
                    images = [node for node in _nodes(converted['blocks']) if node.get('t') == 'Image']
                    self.assertEqual(len(images), 1)
                    self.assertEqual(images[0]['c'][1], caption)
                    self.assertEqual(sum(node == {'t': 'Str', 'c': 'Revised canonical comparison.'}
                        for node in _nodes(converted['blocks'])), 1)
                    self.assertNotIn('Recorded comparison.', json.dumps(converted['blocks']))
                self.assertEqual(revised['figure_exports'], result['figure_exports'])
                self.assertEqual((root / 'canonical' / revised['figure_exports']['original.png']['svg']).read_text(), vector)
                # Two different declared owners for one path must fail before
                # creating a project; retaining the last row would hide a conflict.
                manifest_path = source / 'figures/figures_manifest.json'
                original_manifest = manifest_path.read_bytes()
                manifest_path.write_text(json.dumps({'figures': [
                    {'path': 'original.png', 'caption': 'Recorded comparison.'},
                    {'path': 'original.png', 'caption': 'Other caption.'}]}), encoding='utf-8')
                with self.assertRaisesRegex(ReportExportError, 'Conflicting recorded captions'):
                    export_acm_report(source, root / 'conflict')
                self.assertFalse((root / 'conflict').exists())
                manifest_path.write_bytes(original_manifest)
            self.assertEqual((source / 'report_body.md').read_bytes(), original_body)
            self.assertEqual({path: path.read_bytes() for path in original_inputs}, original_inputs)
            self.assertEqual(result["external_source_references"], ["../../experiment/results.json"])
            self.assertEqual(result['bound_figure_captions'], ['original.png'])
            self.assertFalse(result["external_sources_included"])
            (root / "acm").rename(root / "moved")
            source.rename(root / "original-isolated")
            self.assertIn("figures/figure-1.png", (root / "moved/source.md").read_text())
            self.assertTrue((root / "moved/figures/figure-1.png").is_file())
            saved_vector = result["figure_exports"]["original.png"]["svg"]
            self.assertEqual((root / "moved" / saved_vector).read_text(), vector)
            self.assertFalse((root / "moved/experiment").exists())

    def test_tex_argument_scanner_balances_nested_groups_escapes_and_comments(self) -> None:
        from simple_ar.report.export import _tex_group_end

        arguments = ('{}', '{{{nested}}}', r'{\{escaped\} and \% literal percent}',
            '{before % } ignored closing brace\n{after}}',
            r'[alt={Nested {alt} and ]},width=\linewidth]',
            r'{math \(x_{i}^{2}\) and \href{https://example.org}{a link}}')
        for argument in arguments:
            with self.subTest(argument=argument):
                self.assertEqual(_tex_group_end('prefix' + argument + 'suffix', 6), 6 + len(argument))
        for argument in ('{', '{{nested}', '[alt={unclosed]', '[stray}closing]'):
            with self.subTest(argument=argument), self.assertRaises(ReportExportError):
                _tex_group_end(argument, 0)

    def test_figure_pagination_preserves_common_enclosing_writer_groups(self) -> None:
        from simple_ar.report.export import _paginate_figure_tex, _tex_group_end

        # Identifier-bearing Pandoc figures can enclose both the graphic and
        # caption in a hyperlink target. Nested common scopes must also survive.
        caption = r'\caption{Math \(x_i^2\), \href{https://example.org}{linked text}, \citep{p1}.}\label{fig:plot}'
        for modern in (False, True):
            for nested in (False, True):
                with self.subTest(modern=modern, nested=nested):
                    graphic = r'\includegraphics{figures/plot.pdf}'
                    if modern:
                        graphic = r'\pandocbounded{' + graphic + '}'
                    content = '\n\\centering\n' + graphic + '\n' + caption + '\n'
                    if nested:
                        content = r'\textnormal{' + content + '}'
                    prefix = r'\hypertarget{fig:plot}{'
                    wrapped = prefix + '%\n' + content + '}'
                    source = r'\begin{figure}' + wrapped + r'\end{figure}'
                    allocated = _paginate_figure_tex(source)
                    self.assertEqual(allocated.count(r'\sarfigurecontent'), 1)
                    self.assertEqual(allocated.count(caption), 1)
                    self.assertLess(allocated.index(prefix), allocated.index(r'\sarfigurecontent'))
                    outer_start = allocated.index(prefix) + len(prefix) - 1
                    self.assertEqual(_tex_group_end(allocated, outer_start), allocated.index(r'\end{figure}'))
                    self.assertIn('}{' + graphic + '}{', allocated)

    def test_figure_pagination_uses_both_writer_protocols_and_preserves_caption_tex(self) -> None:
        from simple_ar.report.export import _paginate_figure_tex

        caption = (r'\caption[Short {title}]{Full \emph{caption} with $x_{i}^{2}$, '
            r'\citep{p1}, \href{https://example.org/a}{a link}, and escaped \{braces\}.}'
            '\n' + r'\label{fig:fixture}')
        for modern in (False, True):
            for star, placement in (('', ''), ('', '[p]'), ('*', '[tbp]')):
                for caption_first in (False, True):
                    with self.subTest(modern=modern, star=star, placement=placement, caption_first=caption_first):
                        graphic = r'\includegraphics[width=\linewidth,alt={Nested {alt} and ]}]{figures/a.pdf}'
                        if modern:
                            graphic = r'\pandocbounded{' + graphic + '}'
                        before = '\n\\centering\n' + (caption + '\n' if caption_first else '')
                        after = '\n' + ('' if caption_first else caption + '\n')
                        source = (r'\begin{figure' + star + '}' + placement + before + graphic + after
                            + r'\end{figure' + star + '}')
                        allocated = _paginate_figure_tex(source)
                        self.assertEqual(allocated, r'\begin{figure' + star + '}' + (placement or '[htbp]')
                            + '\n\\sarfigurecontent{' + before + '}{' + graphic + '}{' + after
                            + '}\n' + r'\end{figure' + star + '}')
                        self.assertEqual(allocated.count(caption), 1)
                        self.assertNotIn('[H]', allocated)
        # A float-free document and an ordinary inline image are unchanged.
        inline = r'An inline \includegraphics{figures/a.pdf} without a float.'
        self.assertEqual(_paginate_figure_tex(inline), inline)
        # Refuse unsupported compositions before producing broken brace groups
        # or shrinking subcaptions together with their graphics.
        for graphic in (r'\includegraphics{a.pdf}\includegraphics{b.pdf}',
                r'\href{https://example.org}{\includegraphics{a.pdf}}',
                r'\pandocbounded{\includegraphics{a.pdf} extra}',
                r'\includegraphics[alt={unclosed]{a.pdf}'):
            with self.subTest(graphic=graphic), self.assertRaises(ReportExportError):
                _paginate_figure_tex(r'\begin{figure}' + graphic + caption + r'\end{figure}')

    def test_export_routes_writer_figures_through_page_allocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._source(root)
            def convert(argv, **kwargs):
                if "--to=json" in argv:
                    return json.dumps(_ast())
                return (r'\begin{figure}' + '\n\\centering\n'
                    + r'\includegraphics{figures/a.pdf}\caption{Generated}\end{figure}' + '\n'
                    + r'\begin{figure}[p]\includegraphics{figures/b.pdf}\caption{Explicit}\end{figure}')
            with patch("simple_ar.report.export.shutil.which", return_value="pandoc"), patch("simple_ar.report.export._run", side_effect=convert):
                export_acm_report(source, root / "acm")
            rendered = (root / "acm/body.tex").read_text()
            self.assertIn('\\begin{figure}[htbp]\n\\sarfigurecontent{', rendered)
            self.assertIn('\\begin{figure}[p]\n\\sarfigurecontent{', rendered)
            self.assertEqual(rendered.count(r'\sarfigurecontent'), 2)
            self.assertEqual(rendered.count(r'\caption{Generated}'), 1)
            self.assertEqual(rendered.count(r'\caption{Explicit}'), 1)
            self.assertNotIn("\\begin{figure}[H]", rendered)

    def test_out_of_scope_image_and_unknown_citation_fail_before_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._source(root)
            (root / "outside.png").write_bytes(b"image")
            for document in (_ast(image="../outside.png"), _ast(citation="missing")):
                with self.subTest(document=document), patch("simple_ar.report.export.shutil.which", return_value="pandoc"), patch("simple_ar.report.export._run", return_value=json.dumps(document)):
                    with self.assertRaises(ReportExportError):
                        export_acm_report(source, root / "acm")
                    self.assertFalse((root / "acm").exists())

    def test_existing_export_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._source(root)
            output = root / "acm"
            output.mkdir()
            sentinel = output / "main.tex"
            sentinel.write_text("user edits", encoding="utf-8")
            with self.assertRaises(ReportExportError):
                export_acm_report(source, output)
            self.assertEqual(sentinel.read_text(), "user edits")

    def test_svg_internal_quoted_references_are_not_external_resources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "figure.svg"
            output = root / "export"
            output.mkdir()
            image.write_text('<svg><use href="#shape" style=\'fill:url("#color")\'/></svg>', encoding="utf-8")
            with patch("simple_ar.report.export.shutil.which", return_value="rsvg-convert"), patch("simple_ar.report.export._run") as run:
                self.assertEqual(_copy_image(image, output, 1), "figures/figure-1.pdf")
                run.assert_called_once()
            image.write_text('<svg><image href="https://example.test/image.png"/></svg>', encoding="utf-8")
            with self.assertRaises(ReportExportError):
                _copy_image(image, output, 2)
            image.write_text('<!DOCTYPE svg PUBLIC "SVG" "https://example.test/svg.dtd"><svg><text>Editable</text></svg>', encoding="utf-8")
            self.assertEqual(_copy_image(image, output, 3, convert_svg=False), "figures/figure-3.svg")
            self.assertNotIn("DOCTYPE", (output / "figures/figure-3.svg").read_text())
            self.assertIn("Editable", (output / "figures/figure-3.svg").read_text())
            self.assertIn("DOCTYPE", image.read_text())
            image.write_text('<!DOCTYPE svg [<!ENTITY value "expanded">]><svg><text>&value;</text></svg>', encoding="utf-8")
            with self.assertRaisesRegex(ReportExportError, "external resource"):
                _copy_image(image, output, 4, convert_svg=False)
            image.write_text('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
                             '<image xlink:href="data:image/png;base64,\niVBORw0KGgo="/></svg>', encoding="utf-8")
            self.assertEqual(_copy_image(image, output, 5, convert_svg=False), "figures/figure-5.svg")
            for reference in ('data:image/svg+xml;base64,PHN2Zz4=', 'file:///tmp/image.png',
                              'https://example.test/image.png'):
                image.write_text(f'<svg><image href="{reference}"/></svg>', encoding="utf-8")
                with self.assertRaisesRegex(ReportExportError, "external resource"):
                    _copy_image(image, output, 6, convert_svg=False)
            image.write_text('<svg><use href="data:image/png;base64,aGVsbG8="/></svg>', encoding="utf-8")
            with self.assertRaisesRegex(ReportExportError, "external resource"):
                _copy_image(image, output, 7, convert_svg=False)

    def test_compile_failure_retains_diagnostics_and_limits_file_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("simple_ar.report.export.shutil.which", return_value="tex"), patch("simple_ar.report.export._run", side_effect=ReportExportError("missing package")) as run:
                result = _compile(root, True)
            self.assertEqual(result["status"], "compile_failed")
            self.assertFalse(result["compiled"])
            self.assertIn("missing package", (root / "build.log").read_text())
            self.assertIn("-no-shell-escape", run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs["env"]["openin_any"], "p")
            # A resource refusal is terminal, unlike an ordinary TeX error.
            # Keep its class through compilation and the export manifest so no
            # Poppler/preview work is dispatched after the refusal.
            for stage in ('body', 'standalone', 'pdflatex', 'bibtex'):
                with self.subTest(stage=stage):
                    source = root / stage
                    source.mkdir()
                    (source / 'report_body.md').write_text('# Evidence\n\nBody [@p1].', encoding='utf-8')
                    (source / 'references.bib').write_text('@misc{p1,title={Evidence}}', encoding='utf-8')
                    output = root / (stage + '-export')
                    dispatched = []
                    def render(argv, **kwargs):
                        current = ('standalone' if '--standalone' in argv else
                            'body' if '--to=latex' in argv else argv[0])
                        dispatched.append(current)
                        if current == stage:
                            raise ReportExportResourceError('Process allowance exhausted')
                        if '--to=json' in argv:
                            return json.dumps(_ast())
                        return 'renderer output'
                    with patch('simple_ar.report.export.shutil.which', side_effect=lambda name: name):
                        with self.assertRaises(ReportExportResourceError) as stopped:
                            export_acm_report(source, output, compile_pdf=True, render_preview=True,
                                checks={'max_pages': 12}, runner=render)
                    self.assertEqual(stopped.exception.failure_kind, 'resource_exhausted')
                    manifest = json.loads((output / 'export.json').read_text(encoding='utf-8'))
                    self.assertEqual(manifest['failure_kind'], 'resource_exhausted')
                    self.assertEqual(manifest['status'], 'failed')
                    self.assertFalse(manifest['compiled'])
                    self.assertEqual(dispatched[-1], stage)
                    self.assertNotIn('pdfinfo', dispatched)
                    self.assertFalse(manifest['pdf_preview']['rendered'])
                    if stage in {'pdflatex', 'bibtex'}:
                        self.assertIn('Process allowance exhausted', (output / 'build.log').read_text())


class ProcessArtifactBindingTests(unittest.TestCase):
    """No subprocesses, renderers, models or external services in these tests."""

    def _run(self, workspace, identity, arguments, writes=None, returncode=0):
        from types import SimpleNamespace
        from simple_ar.core.artifacts import write_json

        def execute(spec, **kwargs):
            for name, raw in (writes or {}).items():
                workspace.resolve(name, write=True).write_bytes(raw)
            record = {'status': 'finished', 'returncode': returncode, 'stop_reason': None,
                'invocation_id': identity, 'started_at': identity, 'streams': {},
                'argv': list(spec.argv), 'cwd': str(spec.cwd.resolve()), 'timeout_sec': spec.timeout_sec,
                'output': {'stdout': '', 'stderr': ''}}
            if spec.input_binding is not None:
                record['input_binding'] = spec.input_binding
            write_json(spec.output_dir / 'invocation.json', record)
            return SimpleNamespace(record=record, returncode=returncode, stop_reason=None)

        with patch('simple_ar.tools.workspace.run_process', side_effect=execute) as runner:
            result = workspace.call('run_process', arguments, identity=identity)
            self.assertEqual(runner.call_count, 1)
        return result

    def test_generic_producer_graph_rejects_stale_sources_and_accepts_explicit_new_run(self):
        from simple_ar.tools.workspace import WorkspaceToolGateway

        with tempfile.TemporaryDirectory() as tmp:
            workspace = WorkspaceToolGateway(Path(tmp), {}, None)
            workspace.resolve('source.any').write_bytes(b'v1')
            workspace.resolve('unrelated').write_bytes(b'irrelevant')
            args = {'argv': ['arbitrary-tool'], 'inputs': ['source.any'],
                'outputs': ['middle.any', 'sibling.any']}
            self.assertTrue(self._run(workspace, 'a', args,
                {'middle.any': b'intermediate', 'sibling.any': b'sibling'}).ok)
            second = {'argv': ['another-tool'], 'inputs': ['middle.any', 'sibling.any'], 'outputs': ['figure.png']}
            self.assertTrue(self._run(workspace, 'b', second, {'figure.png': b'old image'}).ok)
            workspace.resolve('unrelated').write_bytes(b'changed')
            with patch.object(workspace, '_check_process_inputs', wraps=workspace._check_process_inputs) as inputs, \
                    patch.object(workspace, '_version_hashes', wraps=workspace._version_hashes) as hashes:
                workspace.check_artifact_producers(['figure.png', 'middle.any', 'sibling.any', 'unrelated'])
                self.assertEqual([call.kwargs['identity'] for call in inputs.call_args_list], ['b', 'a'])
                self.assertEqual(hashes.call_count, 4)  # All inputs and outputs once per record.
                workspace.check_artifact_producers(['figure.png'])
                self.assertEqual(inputs.call_count, 4)  # No implicit cache between calls.
            cache = {}
            with patch.object(workspace, '_artifact_producers', wraps=workspace._artifact_producers) as receipts, \
                    patch.object(workspace, '_version_hashes', wraps=workspace._version_hashes) as hashes:
                workspace.check_artifact_producers(['figure.png'], cache=cache)
                first = hashes.call_count
                workspace.check_artifact_producers(['figure.png'], cache=cache)
                self.assertEqual(receipts.call_count, 1)
                self.assertEqual(hashes.call_count, first)
                workspace.check_artifact_producers(['figure.png'], cache=cache, refresh=True)
                self.assertEqual(receipts.call_count, 2)
                self.assertGreater(hashes.call_count, first)
            # Request only one output; any sibling change must still invalidate it.
            for name in ('source.any', 'middle.any', 'sibling.any'):
                with self.subTest(changed=name):
                    path = workspace.resolve(name)
                    original = path.read_bytes()
                    path.write_bytes(b'tampered')
                    try:
                        with self.assertRaisesRegex(ValueError, 'changed'):
                            workspace.check_artifact_producers(['middle.any'])
                    finally:
                        path.write_bytes(original)
            workspace.resolve('source.any').write_bytes(b'v2')
            cache = {}
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, 'changed'):
                    workspace.check_artifact_producers(['figure.png'], cache=cache)
            self.assertTrue(self._run(workspace, 'c', args, {'middle.any': b'new intermediate'}).ok)
            with self.assertRaisesRegex(ValueError, 'changed'):
                workspace.check_artifact_producers(['figure.png'])
            self.assertTrue(self._run(workspace, 'd', second, {'figure.png': b'new image'}).ok)
            workspace.check_artifact_producers(['figure.png'])
            workspace.resolve('figure.png').write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError, 'changed'):
                workspace.check_artifact_producers(['figure.png'])

    def test_distinct_records_with_same_invocation_id_are_checked_independently(self):
        from simple_ar.core.artifacts import read_json, write_json
        from simple_ar.tools.workspace import WorkspaceToolGateway

        with tempfile.TemporaryDirectory() as tmp:
            workspace = WorkspaceToolGateway(Path(tmp), {}, None)
            for identity in ('a', 'b'):
                args = {'argv': ['tool'], 'outputs': [identity]}
                self.assertTrue(self._run(workspace, identity, args, {identity: b'output'}).ok)
            receipt = Path(tmp) / 'tools/processes/b/invocation.json'
            record = read_json(receipt)
            record['invocation_id'] = 'a'
            write_json(receipt, record)
            with patch.object(workspace, '_check_process_inputs', wraps=workspace._check_process_inputs) as inputs:
                workspace.check_artifact_producers(['a', 'b'])
                self.assertEqual(inputs.call_count, 2)
            workspace.resolve('b').write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError, 'outputs changed'):
                workspace.check_artifact_producers(['a', 'b'])
            record['returncode'] = 1
            write_json(receipt, record)
            with self.assertRaisesRegex(ValueError, 'no successful output binding'):
                workspace.check_artifact_producers(['a', 'b'])

    def test_recovery_preserves_receipt_bytes_and_cannot_backfill_legacy_or_missing_outputs(self):
        from simple_ar.tools.workspace import WorkspaceToolGateway, validate_workspace_arguments

        with tempfile.TemporaryDirectory() as tmp:
            workspace = WorkspaceToolGateway(Path(tmp), {}, None)
            args = {'argv': ['tool'], 'outputs': ['asset.bin']}
            self.assertTrue(self._run(workspace, 'bound', args, {'asset.bin': b'bytes'}).ok)
            receipt = Path(tmp) / 'tools/processes/bound/invocation.json'
            original = receipt.read_bytes()
            with patch('simple_ar.tools.workspace.run_process') as runner, \
                    patch('simple_ar.tools.workspace.settle_process_record'):
                self.assertTrue(workspace.call('run_process', args, identity='bound').ok)
                workspace.resolve('asset.bin').write_bytes(b'different')
                self.assertFalse(workspace.call('run_process', args, identity='bound').ok)
                self.assertFalse(workspace.call('run_process', {'argv': ['other'], 'outputs': ['asset.bin']}, identity='bound').ok)
                runner.assert_not_called()
            self.assertEqual(receipt.read_bytes(), original)
            old = {'argv': ['tool'], 'cwd': 'workspace', 'timeout_sec': 60}
            self.assertEqual(validate_workspace_arguments('run_process', old).model_dump(exclude_none=True), old)
            self.assertIsNone(workspace.process_artifact_binding(old))
            self.assertTrue(self._run(workspace, 'legacy', old).ok)
            legacy = Path(tmp) / 'tools/processes/legacy/invocation.json'
            original = legacy.read_bytes()
            with patch('simple_ar.tools.workspace.run_process') as runner, \
                    patch('simple_ar.tools.workspace.settle_process_record'):
                self.assertFalse(workspace.call('run_process', args, identity='legacy').ok)
                runner.assert_not_called()
            self.assertEqual(legacy.read_bytes(), original)
            self.assertFalse(self._run(workspace, 'missing', {'argv': ['tool'], 'outputs': ['absent']}).ok)
            with self.assertRaisesRegex(ValueError, 'no successful output binding'):
                workspace.check_artifact_producers(['absent'])

    def test_publication_stops_old_image_when_declared_script_changes_without_rendering(self):
        from simple_ar.tools.workspace import WorkspaceToolGateway
        from simple_ar.tools.publication import PublicationToolGateway

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = WorkspaceToolGateway(root, {}, None)
            workspace.resolve('arbitrary.source').write_bytes(b'original')
            workspace.resolve('draft.md').write_text('# Draft\n\n![](figure.png)\n', encoding='utf-8')
            args = {'argv': ['any-producer'], 'inputs': ['arbitrary.source'], 'outputs': ['figure.png']}
            self.assertTrue(self._run(workspace, 'producer', args, {'figure.png': b'image'}).ok)
            workspace.resolve('arbitrary.source').write_bytes(b'user edit')
            gateway = PublicationToolGateway(root, workspace, None)
            with patch('simple_ar.tools.publication.export_acm_report') as exporter, \
                    patch('simple_ar.tools.publication.run_process') as runner:
                result = gateway.call('export_report', {'markdown': 'draft.md', 'compile_pdf': False}, identity='export')
                self.assertFalse(result.ok)
                self.assertIn('producer inputs/outputs changed', result.error)
                exporter.assert_not_called()
                runner.assert_not_called()
            self.assertFalse((root / 'tools/publications/export/request.json').exists())

    def test_failed_latest_producer_and_unsafe_declarations_do_not_certify_outputs(self):
        from simple_ar.tools.workspace import WorkspaceToolGateway

        with tempfile.TemporaryDirectory() as tmp:
            workspace = WorkspaceToolGateway(Path(tmp), {}, None)
            args = {'argv': ['tool'], 'outputs': ['asset']}
            self.assertTrue(self._run(workspace, 'a', args, {'asset': b'image'}).ok)
            self.assertFalse(self._run(workspace, 'b', args, returncode=1).ok)
            with self.assertRaisesRegex(ValueError, 'no successful output binding'):
                workspace.check_artifact_producers(['asset'])
            with patch('simple_ar.tools.workspace.run_process') as runner:
                for declaration in ({'inputs': ['missing']}, {'outputs': ['../escape']}, {'outputs': ['workspace']}):
                    self.assertFalse(workspace.call('run_process', {'argv': ['tool'], **declaration}, identity='bad').ok)
                runner.assert_not_called()

    def test_tied_timestamps_reject_and_self_mutation_cannot_hide_in_cycle(self):
        from simple_ar.core.artifacts import read_json, write_json
        from simple_ar.tools.workspace import WorkspaceToolGateway

        with tempfile.TemporaryDirectory() as tmp:
            workspace = WorkspaceToolGateway(Path(tmp), {}, None)
            args = {'argv': ['tool'], 'outputs': ['asset']}
            self.assertTrue(self._run(workspace, 'z', args, {'asset': b'image'}).ok)
            self.assertFalse(self._run(workspace, 'a', args, returncode=1).ok)
            for identity in ('z', 'a'):
                receipt = Path(tmp) / f'tools/processes/{identity}/invocation.json'
                record = read_json(receipt)
                record['started_at'] = '2026-10-20T00:00:00.000000+00:00'
                write_json(receipt, record)
            with self.assertRaisesRegex(ValueError, 'Ambiguous producer timestamps'):
                workspace.check_artifact_producers(['asset'])
            workspace.resolve('self').write_bytes(b'input')
            self_args = {'argv': ['tool'], 'inputs': ['self'], 'outputs': ['self']}
            self.assertFalse(self._run(workspace, 'self', self_args, {'self': b'mutated'}).ok)
            with self.assertRaisesRegex(ValueError, 'changed'):
                workspace.check_artifact_producers(['self'])


if __name__ == "__main__":
    unittest.main()
