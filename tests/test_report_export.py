from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from simple_ar.report.export import ReportExportError, _compile, _copy_image, export_acm_report


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


class ReportExportTests(unittest.TestCase):
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
                    for container in ('Div', 'BlockQuote', 'BulletList'):
                        children = [deepcopy(figure), deepcopy(duplicate), deepcopy(discussion)]
                        content = ([['topic', [], []], children] if container == 'Div' else
                            [children] if container == 'BulletList' else children)
                        wrapped = {'t': container, 'c': content}
                        nested = {'blocks': [wrapped]}
                        self.assertEqual(_bind_figure_captions(nested, {'figures/raw.svg': caption}),
                            ['figures/raw.svg'])
                        self.assertEqual(len(children), 2)
                        self.assertEqual(children[-1], discussion)

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

    def test_technical_text_wraps_without_rewriting_source_math_or_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._source(root)
            document = _ast()
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
                else:
                    markdown.append(data)
                return "converted text"
            with patch("simple_ar.report.export.shutil.which", return_value="pandoc"), patch(
                    "simple_ar.report.export._run", side_effect=convert):
                export_acm_report(source, root / "acm")
            para = converted[1]["blocks"][-2]["c"]
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
            self.assertEqual(converted[1]['blocks'][-1]['c'], ['latex',
                r'\VerbatimInput[breaklines=true,breakanywhere=true,breaksymbolleft={},breaksymbolright={},breakanywheresymbolpre={},breakanywheresymbolpost={}]{code/block-1.txt}'])
            self.assertEqual((root / 'acm/code/block-1.txt').read_text(), code)
            self.assertEqual(markdown[0]['blocks'][-2:], document['blocks'][-2:])
            self.assertIn(r'\usepackage{fvextra,seqsplit,etoolbox,xparse}', (root / 'acm/main.tex').read_text())
            self.assertIn("Missing authors/dates remain missing", (root / "acm/README.txt").read_text())

    def _source(self, root: Path) -> Path:
        report = root / "report"
        report.mkdir()
        (report / "report_body.md").write_text("# Evidence report\n\nBody [@p1].\n", encoding="utf-8")
        (report / "references.bib").write_text("@misc{p1, title={Evidence}}\n", encoding="utf-8")
        return report

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
                return "converted text"
            with patch("simple_ar.report.export.shutil.which", return_value="pandoc"), patch("simple_ar.report.export._run", side_effect=convert):
                result = export_acm_report(source, root / "acm")
            self.assertEqual(result["status"], "exported")
            self.assertFalse(result["compiled"])
            self.assertFalse(result["quality_checked"])
            self.assertNotIn('pdf_preview', result)  # Default exports keep their historical shape.
            self.assertTrue(all(document["meta"] == {} for document in calls))
            body_headers = [block["c"][0] for block in calls[1]["blocks"] if block["t"] == "Header"]
            self.assertEqual(body_headers, [1])
            self.assertEqual((root / "acm/source.md").read_text(), "converted text")
            self.assertEqual((source / "report_body.md").read_text(), "# Evidence report\n\nBody [@p1].\n")
            self.assertEqual((root / "acm/references.bib").read_text(), (source / "references.bib").read_text())
            main = (root / "acm/main.tex").read_text()
            self.assertLess(main.index("\\begin{abstract}"), main.index("\\maketitle"))
            self.assertIn("\\authorsaddresses{}", main)
            self.assertIn("\\microtypesetup{expansion=false}", main)
            self.assertIn("\\RenewDocumentCommand{\\bibitem}{o m}", main)
            self.assertIn("\\saroriginalbibitem[{#1}]{#2}", main)
            self.assertNotIn('brace the full optional label', (root / "acm/README.txt").read_text())
            self.assertIn("full CM-Super", (root / "acm/README.txt").read_text())
            self.assertIn("\\Gin@nat@width>\\linewidth", main)
            self.assertIn("\\Gin@nat@height>0.8\\textheight", main)
            self.assertIn("\\setkeys{Gin}{width=\\sarmaxwidth,height=\\sarmaxheight,keepaspectratio}", main)
            self.assertNotIn("\\setkeys{Gin}{width=\\linewidth,keepaspectratio}", main)
            self.assertTrue((root / "acm/export.json").is_file())
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
                        'status': 'compile_failed', 'compiled': False, 'error': 'Renderer failure'}):
                    failed_delivery = gateway.call('export_report', delivery_request, identity='failed-delivery')
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
                for historic in (saved, input_only, markdown_only):
                    with self.subTest(historic=historic):
                        (owner / 'request.json').write_text(json.dumps(historic), encoding='utf-8')
                        legacy = gateway.call('export_report', bound_request, identity='bound')
                        self.assertEqual(legacy.status, 'blocked', legacy.error)
                        self.assertEqual(legacy.data['historical_manifest'], manifest)
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
            document['blocks'].append({'t': 'Para', 'c': [{'t': 'Emph', 'c': caption}]})
            document["blocks"].append({"t": "Para", "c": [{"t": "Link", "c": [
                ["", [], []], [{"t": "Str", "c": "Measured source"}], ["../../experiment/results.json", ""],
            ]}]})
            def convert(argv, **kwargs):
                if "--to=json" in argv:
                    if kwargs['text'] in {'Recorded comparison.', 'Other caption.'}:
                        return json.dumps({'blocks': [{'t': 'Para', 'c':
                            [{'t': 'Str', 'c': kwargs['text']}]}]})
                    return json.dumps(document)
                data = json.loads(kwargs["text"])
                if "--to=markdown" in argv:
                    image = data["blocks"][-2]["c"][0]["c"][-1][0]
                    self.assertEqual(data['blocks'][-2]['c'][0]['c'][1], caption)
                    self.assertFalse(any(row.get('t') == 'Para' and row.get('c') ==
                        [{'t': 'Emph', 'c': caption}] for row in data['blocks']))
                    self.assertEqual(data["blocks"][-1]["c"][0]["t"], "Span")
                    return f"![Measured figure]({image})\n"
                return "converted text"
            original_body = (source / 'report_body.md').read_bytes()
            with patch("simple_ar.report.export.shutil.which", return_value="pandoc"), patch("simple_ar.report.export._run", side_effect=convert):
                result = export_acm_report(source, root / "acm")
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

    def test_generated_figure_placement_prefers_source_location_without_forcing_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._source(root)
            def convert(argv, **kwargs):
                if "--to=json" in argv:
                    return json.dumps(_ast())
                return "\\begin{figure}\nGenerated\\end{figure}\n\\begin{figure}[p]\nExplicit\\end{figure}"
            with patch("simple_ar.report.export.shutil.which", return_value="pandoc"), patch("simple_ar.report.export._run", side_effect=convert):
                export_acm_report(source, root / "acm")
            rendered = (root / "acm/body.tex").read_text()
            self.assertIn("\\begin{figure}[htbp]\nGenerated", rendered)
            self.assertIn("\\begin{figure}[p]\nExplicit", rendered)
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


if __name__ == "__main__":
    unittest.main()
