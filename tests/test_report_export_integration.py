"""Opt-in real renderer/TeX check; no model, network or dependency installation."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

from simple_ar.report.export import ReportExportError, export_acm_report, _compile, _nodes, _run


IDENTIFIER = 'Za71c9' * 32
INLINE_COMMAND = 'python -m runner --input "data folder/table.csv"'
LONG_PATH = 'artifacts/' + 'nested_measurement_directory/' * 12 + 'samples.csv'
LITERAL_CODE = ('analyze --input ' + LONG_PATH + ' --label ' + IDENTIFIER + '\n'
    + '\t' + r'\end{Verbatim}\input{missing-file} % $ & # _ ^ ~ {}')
TECHNICAL_BODY = ('\n`' + INLINE_COMMAND + '`\n\nInline identifier `' + IDENTIFIER + '`; plain identifier ' + IDENTIFIER
    + '. Inline path `' + LONG_PATH + '`.\n\n```sh\n' + LITERAL_CODE + '\n```\n')


@unittest.skipUnless(os.environ.get("SAR_TEST_EXPORT_INTEGRATION") == "1" and
    all(shutil.which(name) for name in ("pandoc", "rsvg-convert", "pdflatex", "bibtex")),
    "Opt-in integration requires Pandoc, librsvg and acmart-equipped TeX")
class PortableExportIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(all(shutil.which(name) for name in ('pdftotext', 'pdfinfo', 'pdftoppm')),
        'Caption pagination checks require Poppler')
    def test_caption_page_budget_preserves_native_text_and_clean_rebuild(self):
        from pypdf import PdfReader

        # Enough prose to exceed the retired 20% caption allowance at ordinary
        # font sizes. The tenth figure also exercises a wider number label.
        caption = ('Longmarker. Measured $x_i^2$ and [data portal](https://example.org/data) '
            'with evidence [@p1]. ' + ' '.join(
                f'Observation {index:03d} preserves measured data and uncertainty.'
                for index in range(1, 37)) + ' Captionendmarker.')
        for layout in ('builtin', 'supplied'):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / 'input'
                source.mkdir()
                body = '# Caption allocation\n\n## Results\n\nNative text and figures.\n\n'
                body += '\n\n'.join(f'![Small fixture {index}.](tiny.svg)' for index in range(1, 10))
                body += '\n\n![' + caption + '](portrait.svg){#fig:long}\n\n![Shortmarker.](tiny.svg)\n'
                (source / 'report_body.md').write_text(body, encoding='utf-8')
                bibliography = '@misc{p1, author={Example, A}, year={2024}, title={Caption evidence}}\n'
                (source / 'references.bib').write_text(bibliography, encoding='utf-8')
                for filename, width, height in (('tiny.svg', 32, 48), ('portrait.svg', 2400, 4800)):
                    (source / filename).write_text(
                        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
                        f'<rect x="1" y="1" width="{width - 2}" height="{height - 2}" '
                        'fill="white" stroke="black"/></svg>', encoding='utf-8')
                options = {}
                if layout == 'supplied':
                    # Deliberately different page area, body/caption fonts and
                    # spacing; all are set AFTER the renderer's preamble hook.
                    (source / 'layout.latex').write_text(
                        '\\documentclass[12pt]{article}\n'
                        '$for(header-includes)$\n$header-includes$\n$endfor$\n'
                        '\\usepackage[paperwidth=7in,paperheight=9in,margin=.75in]{geometry}\n'
                        '\\usepackage{natbib,hyperref,amsmath,caption}\n'
                        '\\captionsetup{font=small,skip=14pt}\n'
                        '\\title{$title$}\n\\begin{document}\n\\maketitle\n$body$\n'
                        '\\bibliographystyle{plainnat}\n\\bibliography{references}\n'
                        '\\end{document}\n', encoding='utf-8')
                    options['template'] = {'path': 'layout.latex'}
                output = root / 'export'
                qa_root = os.environ.get('SAR_TEST_EXPORT_QA_DIR')
                retained = None
                if qa_root:
                    Path(qa_root).mkdir(parents=True, exist_ok=True)
                    retained = Path(tempfile.mkdtemp(prefix='caption-' + layout + '-', dir=qa_root))

                def render(argv, **kwargs):
                    rendered = _run(argv, **kwargs)
                    if retained and '--from=json' in argv and '--to=latex' in argv and '--standalone' not in argv:
                        # Retain the actual writer protocol even if pagination
                        # subsequently rejects it; never repair its TeX in QA.
                        (retained / 'actual-pandoc-body.tex').write_text(rendered, encoding='utf-8')
                    return rendered

                result = export_acm_report(source, output, compile_pdf=True, render_preview=True,
                    caption_owner='markdown', runner=render, **options)
                if retained:
                    shutil.copytree(output, retained / 'project')
                    print('P3 caption evidence:', retained / 'project', flush=True)
                self.assertTrue(result['compiled'], result)
                self.assertFalse(any('Float too large' in warning or 'Overfull \\vbox' in warning
                    or 'undefined citation' in warning.lower() for warning in result['compile_warnings']), result)
                self.assertEqual((source / 'report_body.md').read_text(encoding='utf-8'), body)
                self.assertEqual((output / 'references.bib').read_text(encoding='utf-8'), bibliography)
                rendered = (output / 'body.tex').read_text(encoding='utf-8')
                self.assertEqual(rendered.count(r'\sarfigurecontent'), 11)
                # Modern writers also carry caption text in image alt keys.
                self.assertEqual(rendered.count(r'\caption'), 11)

                def check_pdf(project):
                    text = subprocess.check_output(['pdftotext', str(project / 'main.pdf'), '-'], text=True)
                    normalized = ' '.join(text.split())
                    self.assertEqual(normalized.count('Longmarker.'), 1)
                    self.assertEqual(normalized.count('Captionendmarker.'), 1)
                    self.assertIn('data portal', normalized)
                    self.assertNotIn('[?]', normalized)
                    self.assertRegex(normalized, r'(?:Figure|Fig\.)\s*10[.:]')
                    for index in range(1, 37):
                        self.assertRegex(normalized, r'Observation\s+' + f'{index:03d}')
                    bbox = subprocess.check_output(['pdftotext', '-bbox', str(project / 'main.pdf'), '-'], text=True)
                    ns = '{http://www.w3.org/1999/xhtml}'
                    pages = list(ET.fromstring(bbox).iter(ns + 'page'))
                    marked = {marker: [(page, word) for page in pages for word in page.iter(ns + 'word')
                        if word.text == marker] for marker in ('Longmarker.', 'Captionendmarker.', 'Shortmarker.')}
                    self.assertTrue(all(len(matches) == 1 for matches in marked.values()), marked)
                    start_page, start = marked['Longmarker.'][0]
                    end_page, end = marked['Captionendmarker.'][0]
                    self.assertIs(start_page, end_page, 'Caption must travel with its figure on one page')
                    self.assertGreater(float(end.attrib['yMax']), float(start.attrib['yMin']))
                    self.assertGreater(float(end.attrib['yMax']) - float(start.attrib['yMin']),
                        .2 * float(start_page.attrib['height']),
                        'Fixture must exceed the retired fixed caption allowance')
                    _, short = marked['Shortmarker.'][0]
                    long_font = float(start.attrib['yMax']) - float(start.attrib['yMin'])
                    short_font = float(short.attrib['yMax']) - float(short.attrib['yMin'])
                    self.assertAlmostEqual(long_font, short_font, delta=.15,
                        msg='Long caption must retain the template caption font')
                    for page in pages:
                        for word in page.iter(ns + 'word'):
                            self.assertGreaterEqual(float(word.attrib['yMin']), 0)
                            self.assertLessEqual(float(word.attrib['yMax']), float(page.attrib['height']))
                    # The custom layout's bottom text boundary is known, unlike
                    # a paper-edge-only check that can miss footer collisions.
                    if layout == 'supplied':
                        self.assertLessEqual(float(end.attrib['yMax']), 9 * 72 - .75 * 72 + 1)
                    reader = PdfReader(project / 'main.pdf')
                    actions = [annotation.get_object().get('/A') for page in reader.pages
                        for annotation in page.get('/Annots', [])]
                    links = [action.get_object().get('/URI') for action in actions if action is not None]
                    self.assertIn('https://example.org/data', links)
                    auxiliary = (project / 'main.aux').read_text(encoding='utf-8')
                    self.assertRegex(auxiliary, r'\\newlabel\{fig:long\}\{\{10\}')
                    self.assertEqual(auxiliary.count(r'\newlabel{fig:long}'), 1)

                check_pdf(output)
                # Rebuild from delivered editable inputs only; the original
                # manuscript and every first-build auxiliary are unavailable.
                moved = root / 'relocated'
                moved.mkdir()
                for filename in result['public_artifacts']:
                    if filename == 'main.pdf':
                        continue  # Require a clean build, not the first PDF.
                    target = moved / filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(output / filename, target)
                self.assertFalse((moved / 'publication_view.json').exists())
                self.assertFalse((moved / 'main.aux').exists())
                source.rename(root / 'withheld-input')
                rebuilt = _compile(moved, True)
                self.assertTrue(rebuilt['compiled'], rebuilt)
                check_pdf(moved)

    def test_caption_that_exceeds_page_fails_with_editable_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'input'
            source.mkdir()
            caption = ' '.join('Measured values retain their uncertainty and original interpretation.'
                for _ in range(300))
            (source / 'report_body.md').write_text('# Oversize caption\n\n![' + caption + '](plot.svg)\n',
                encoding='utf-8')
            (source / 'plot.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
                '<rect width="100" height="100" fill="black"/></svg>', encoding='utf-8')
            result = export_acm_report(source, root / 'export', compile_pdf=True, caption_owner='markdown')
            self.assertFalse(result['compiled'], result)
            self.assertEqual(result['status'], 'compile_failed')
            self.assertIn('Figure caption cannot fit on one page', result['compile_note'])
            self.assertIn('Figure caption cannot fit on one page', (root / 'export/build.log').read_text())
            self.assertIn('original interpretation', ' '.join((root / 'export/body.tex').read_text().split()))
            self.assertTrue((root / 'export/main.tex').is_file())

    def _assert_fixture_table_layout(self, pages):
        """Check cell wrapping spatially, not just token survival on a page."""
        ns = '{http://www.w3.org/1999/xhtml}'
        rows = [
            ['Case one', 'Short input', 'Notes saved', 'Check open', 'Local file', 'Plain text', 'Read notes'],
            ['Case two', 'Full input', 'Notes ready', 'Check done', 'Local data', 'Saved text', 'Keep notes'],
        ]
        anchors = [(page, word) for page in pages for word in page.iter(ns + 'word') if word.text == 'Case']
        self.assertEqual(len(anchors), 2, 'Both fixture row labels must survive')
        # This two-row fixture is short enough to fit together. A split here
        # needs layout inspection rather than silently dropping spatial QA.
        self.assertIs(anchors[0][0], anchors[1][0], 'Small fixture table unexpectedly split across pages')
        tops = [float(word.attrib['yMin']) for _, word in anchors]
        row_height = tops[1] - tops[0]
        self.assertGreater(row_height, 0)
        for (page, _), top, expected in zip(anchors, tops, rows, strict=True):
            words = [word for word in page.iter(ns + 'word')
                     if top - 1 <= float(word.attrib['yMin']) < top + row_height - 1]
            cells = []
            for phrase in expected:
                selected = []
                for token in phrase.split():
                    matches = [word for word in words if word.text == token]
                    self.assertEqual(len(matches), 1, f'Missing/duplicated table token {token!r} in row {expected[0]}')
                    selected.append(matches[0])
                cells.append((min(float(w.attrib['xMin']) for w in selected),
                              max(float(w.attrib['xMax']) for w in selected)))
            for left, right in zip(cells, cells[1:]):
                self.assertLess(left[1], right[0], 'Adjacent table cell text overlaps or columns changed order')

    @unittest.skipUnless(all(shutil.which(name) for name in ('pdfinfo', 'pdftoppm', 'pdftotext')),
        'Supplied-template previews require Poppler')
    def test_supplied_article_and_titlepage_templates_compile_and_preview(self):
        # Both required layouts use the baseline pdfLaTeX toolchain, including
        # the current Greek/Latin helper. Optional XeLaTeX never gates them.
        layouts = [
            ('article', 'pdflatex', None, '\\documentclass{article}\n\\usepackage[T1]{fontenc}\n',
             '\\maketitle\n$if(abstract)$\\begin{abstract}$abstract$\\end{abstract}$endif$\n'),
            ('titlepage', 'pdflatex', 'chapter', '\\documentclass{report}\n\\usepackage[T1]{fontenc}\n',
             '\\begin{titlepage}\\centering\\Huge $title$\\end{titlepage}\n'),
        ]
        if shutil.which('xelatex'):
            # fontspec's TeX-distribution default covers Latin accents. This
            # optional case makes no system-font, Greek-glyph or CJK promise.
            layouts.append(('optional-xelatex', 'xelatex', None,
                '\\documentclass{article}\n\\usepackage{fontspec}\n', layouts[0][4]))
        for name, engine, division, preamble, opening in layouts:
            with self.subTest(layout=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / 'input'
                source.mkdir()
                body = ('# Résumé α\n\n## Abstract\n\nUnicode β ≤ Γ fixture.\n\n'
                    if engine == 'pdflatex' else '# Résumé\n\n## Abstract\n\nUnicode café fixture.\n\n')
                body += ('## Results\n\nCitation [@p1].\n\n'
                    '| Record label | Input scope | Owner notes | Review status | Source type | Output format | Next action |\n'
                    '| :--- | :---: | ---: | --- | :--- | :---: | ---: |\n'
                    '| Case one | Short input | Notes saved | Check open | Local file | Plain text | Read notes |\n'
                    '| Case two | Full input | Notes ready | Check done | Local data | Saved text | Keep notes |\n\n'
                    # The grid's authored boundaries give the real reader
                    # nondefault widths; they must survive the TeX AST pass.
                    '+----------------+----------------------------------------+\n'
                    f"| {'Label':<14} | {'Description':<38} |\n"
                    '+================+========================================+\n'
                    f"| {'First':<14} | {'A brief note set by the author.':<38} |\n"
                    f"| {'':<14} | {'More detail follows here.':<38} |\n"
                    '+----------------+----------------------------------------+\n\n'
                    '![Bounded fixture](plot.svg)\n') + TECHNICAL_BODY
                (source / 'report_body.md').write_text(body, encoding='utf-8')
                (source / 'references.bib').write_text('@misc{p1, title={Fixture}, author={Example, A}, year={2024}}',
                    encoding='utf-8')
                # Oversized portrait vector exercises both width and height
                # bounds. Large native labels remain legible when fitted.
                image = source / 'plot.svg'
                image.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="2400" height="4800" viewBox="0 0 2400 4800">'
                    '<rect width="2400" height="4800" fill="white"/>'
                    '<g fill="none" stroke="black" stroke-width="12">'
                    '<rect x="400" y="300" width="1600" height="600"/>'
                    '<path d="M1200 900 L1200 3900"/>'
                    '<rect x="400" y="3900" width="1600" height="600"/></g>'
                    '<g font-size="144" text-anchor="middle">'
                    '<text x="1200" y="650">Input</text><text x="1200" y="4250">Output</text></g></svg>', encoding='utf-8')
                original_svg = image.read_bytes()
                template = source / 'layout.latex'
                template.write_text(preamble + '$for(header-includes)$\n$header-includes$\n$endfor$\n'
                    '\\usepackage{natbib,hyperref}\n'
                    '\\title{$title$}\n\\begin{document}\n' + opening + '\n$body$\n'
                    '$if(bibliography)$\\bibliographystyle{$biblio-style$}\n'
                    '\\bibliography{references}$endif$\n\\end{document}\n', encoding='utf-8')
                options = {'path': template.name, 'engine': engine}
                if division:
                    options['top_level_division'] = division
                with self.assertRaisesRegex(ReportExportError, 'Missing or out-of-scope template'):
                    export_acm_report(source, root / 'missing-companion',
                        template={**options, 'assets': ['missing.sty']})
                self.assertFalse((root / 'missing-companion').exists())
                image.rename(source / 'withheld.svg')
                with self.assertRaisesRegex(ReportExportError, 'Missing or out-of-scope report image'):
                    export_acm_report(source, root / 'missing-image', template=options)
                self.assertFalse((root / 'missing-image').exists())
                (source / 'withheld.svg').rename(image)
                output = root / 'publication'
                reader_tables, writer_tables, latex_documents = [], [], []

                def columns(node):
                    if len(node['c']) == 5:
                        return node['c'][1], [width or None for width in node['c'][2]]
                    return ([alignment for alignment, _ in node['c'][2]],
                        [width.get('c') for _, width in node['c'][2]])

                def render(argv, **kwargs):
                    # Observe the real Pandoc reader and writer contract; no
                    # TeX substitution or synthetic table conversion here.
                    if '--from=json' in argv and '--to=latex' in argv:
                        document = json.loads(kwargs['text'])
                        latex_documents.append(document)
                        writer_tables.extend(columns(node) for node in _nodes(document)
                            if node.get('t') == 'Table')
                    rendered = _run(argv, **kwargs)
                    if '--to=json' in argv:
                        reader_tables.extend(columns(node) for node in _nodes(json.loads(rendered))
                            if node.get('t') == 'Table')
                    return rendered

                result = export_acm_report(source, output, template=options,
                    compile_pdf=True, render_preview=True, caption_owner='markdown', runner=render,
                    checks={'max_pages': 12, 'forbidden_identifiers': ['Résumé', 'Unlisted Person']})
                # Optional server evidence survives TemporaryDirectory cleanup,
                # including any later assertion failure. No rendering is added.
                qa_root = os.environ.get('SAR_TEST_EXPORT_QA_DIR')
                qa_evidence = None
                if qa_root:
                    Path(qa_root).mkdir(parents=True, exist_ok=True)
                    qa_evidence = Path(tempfile.mkdtemp(prefix=name + '-', dir=qa_root))
                    shutil.copytree(output, qa_evidence / 'project')
                    print('P3 layout evidence:', qa_evidence / 'project', flush=True)
                self.assertTrue(result['compiled'], result)
                self.assertEqual(len(latex_documents), 2)  # body and standalone only
                self.assertEqual(latex_documents[-1]['meta']['title']['t'], 'MetaInlines')
                self.assertNotIn('abstract', latex_documents[-1]['meta'])  # supplied layout keeps authored body
                self.assertTrue(result['pdf_preview']['rendered'], result)
                self.assertFalse(result['quality_checked'])
                self.assertEqual(result['compliance']['anonymity'], 'unverified')
                self.assertEqual(result['status'], 'checks_failed')
                checked = result['checks']['results']
                self.assertEqual(checked['max_pages']['status'], 'passed')
                self.assertEqual(checked['forbidden_identifiers']['status'], 'failed')
                self.assertTrue(any(row['literal'] == 'Résumé' for row in checked['forbidden_identifiers']['matches']))
                self.assertIn('Résumé', (output / checked['forbidden_identifiers']['evidence']).read_text(encoding='utf-8'))
                self.assertFalse(any('undefined citation' in warning.lower() or 'Missing character:' in warning
                    for warning in result['compile_warnings']), result)
                # Keep TeX warnings for independent layout review. A slight
                # overfull box is not synonymous with a cropped page; authored
                # widths also remain authoritative. Check actual page bounds,
                # not a requirement that every compiler warning disappear.
                bbox = subprocess.check_output(['pdftotext', '-bbox', str(output / 'main.pdf'), '-'], text=True)
                if qa_evidence:
                    (qa_evidence / 'original-bbox.xhtml').write_text(bbox, encoding='utf-8')
                pages = list(ET.fromstring(bbox).iter('{http://www.w3.org/1999/xhtml}page'))
                self.assertTrue(pages)
                self.assertEqual(checked['max_pages']['page_count'], len(pages))
                self.assertTrue(any(list(page.iter('{http://www.w3.org/1999/xhtml}word')) for page in pages))
                self._assert_fixture_table_layout(pages)
                for page in pages:
                    width, height = float(page.attrib['width']), float(page.attrib['height'])
                    for word in page.iter('{http://www.w3.org/1999/xhtml}word'):
                        self.assertGreaterEqual(float(word.attrib['xMin']), 0)
                        self.assertGreaterEqual(float(word.attrib['yMin']), 0)
                        self.assertLessEqual(float(word.attrib['xMax']), width)
                        self.assertLessEqual(float(word.attrib['yMax']), height)
                self.assertEqual(len(reader_tables), 2)
                self.assertEqual(len(writer_tables), 2)
                self.assertTrue(all(width is not None for width in reader_tables[1][1]))
                # Reader versions may infer widths for a wide pipe table.
                # Respect its actual ColSpecs, not assumptions based on how
                # the Markdown fixture happens to look.
                for original, converted in zip(reader_tables, writer_tables, strict=True):
                    self.assertEqual(converted[0], original[0])
                    expected = ([1 / len(original[1])] * len(original[1])
                        if all(width is None for width in original[1]) else original[1])
                    self.assertEqual(converted[1], expected)
                self.assertEqual((output / 'body.tex').read_text(encoding='utf-8').count(r'\begin{longtable}'), 2)
                # Both supplied templates preserve the authored Abstract in
                # the body, including the layout with no abstract slot at all.
                self.assertIn('Unicode', (output / 'body.tex').read_text(encoding='utf-8'))
                self.assertIn('fixture.', (output / checked['forbidden_identifiers']['evidence']).read_text(encoding='utf-8'))
                self.assertEqual((source / 'report_body.md').read_text(encoding='utf-8'), body)
                self.assertEqual(image.read_bytes(), original_svg)
                self.assertEqual((output / 'figures/figure-1.svg').read_bytes(), original_svg)
                self.assertEqual((output / 'template.latex').read_bytes(), template.read_bytes())
                self.assertEqual((output / 'code/block-1.txt').read_bytes(), LITERAL_CODE.encode('utf-8'))
                main = (output / 'main.tex').read_text(encoding='utf-8')
                # Supplied layouts retain the authored Abstract section. Their
                # optional abstract slot is empty and must emit no second heading.
                self.assertNotIn(r'\begin{abstract}', main)
                self.assertEqual((output / 'body.tex').read_text(encoding='utf-8').count('{Abstract}'), 1)
                pdf_text = (output / checked['forbidden_identifiers']['evidence']).read_text(encoding='utf-8')
                self.assertEqual(len(re.findall(r'\bAbstract\b', pdf_text)), 1)
                self.assertNotIn('acmart', main)
                self.assertIn('\\input{body.tex}', main)
                if division:
                    self.assertIn(r'\chapter{Results}', (output / 'body.tex').read_text(encoding='utf-8'))
                self.assertNotIn('$header-includes$', main)
                self.assertIn('\\usepackage{fvextra,seqsplit,etoolbox}', main)
                self.assertEqual(main.count(r'\def\sarmaxwidth'), 1)
                self.assertIn(r'\setkeys{Gin}{width=\sarmaxwidth,height=\sarmaxheight,keepaspectratio}', main)
                self.assertEqual('\\DeclareUnicodeCharacter{03B1}' in main, engine == 'pdflatex')
                # Exercise relocation within this same template conversion,
                # rather than creating another layout/conversion matrix.
                source.rename(root / 'withheld-input')
                moved = root / 'relocated-publication'
                output.rename(moved)
                for filename in ('main.pdf', 'main.aux', 'main.bbl', 'main.blg', 'main.out', 'main.log', 'main.toc'):
                    (moved / filename).unlink(missing_ok=True)
                rebuilt = _compile(moved, True, engine=engine)
                self.assertTrue(rebuilt['compiled'], rebuilt)
                self.assertFalse(any('undefined citation' in warning.lower() or 'Missing character:' in warning
                    for warning in rebuilt['compile_warnings']), rebuilt)
                moved_text = subprocess.check_output(['pdftotext', str(moved / 'main.pdf'), '-'], text=True)
                for retained in ('fixture.', 'Results', 'Case one'):
                    self.assertIn(retained, ' '.join(moved_text.split()))
                moved_bbox = subprocess.check_output(['pdftotext', '-bbox', str(moved / 'main.pdf'), '-'], text=True)
                if qa_evidence:
                    (qa_evidence / 'rebuilt-bbox.xhtml').write_text(moved_bbox, encoding='utf-8')
                    shutil.copy2(moved / 'main.pdf', qa_evidence / 'rebuilt-main.pdf')
                    shutil.copy2(moved / 'build.log', qa_evidence / 'rebuilt-build.log')
                self._assert_fixture_table_layout(list(ET.fromstring(moved_bbox).iter('{http://www.w3.org/1999/xhtml}page')))

    @unittest.skipUnless(all(shutil.which(name) for name in ('pdfinfo', 'pdftoppm')),
        'PDF preview integration requires Poppler')
    def test_preview_of_compiled_canonical_body_moves_with_project(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / 'input'
            original.mkdir()
            body = '# Canonical body\n\n## Abstract\n\nMetadata β ≤ Γ fixture.\n\n## Result ' + IDENTIFIER + '\n\nBounded renderer fixture.\n' + TECHNICAL_BODY
            (original / 'report_body.md').write_text(body, encoding='utf-8')
            output = root / 'export'
            manifest = export_acm_report(original, output,
                title='A descriptive Ω title with ordinary spaces and a technical identifier ' + IDENTIFIER,
                compile_pdf=True, render_preview=True)
            self.assertEqual(manifest['status'], 'compiled', manifest)
            self.assertTrue(manifest['compiled'])
            main = (output / 'main.tex').read_text(encoding='utf-8')
            # Ω appears only in the explicit title, not the source Markdown.
            for code in ('03A9', '03B2', '2264', '0393'):
                self.assertIn('\\DeclareUnicodeCharacter{' + code + '}', main)
            self.assertFalse(any('Overfull \\hbox' in row for row in manifest['compile_warnings']), manifest)
            self.assertTrue(manifest['pdf_preview']['rendered'])
            self.assertFalse(manifest['quality_checked'])
            self.assertFalse(manifest['pdf_preview']['visual_reviewed'])
            count = int(subprocess.check_output(['pdfinfo', str(output / 'main.pdf')],
                text=True, env={**os.environ, 'LC_ALL': 'C'}).split('Pages:')[1].split()[0])
            self.assertEqual(len(manifest['pdf_preview']['pages']), count)
            self.assertEqual((original / 'report_body.md').read_text(), body)
            moved = root / 'moved'
            output.rename(moved)
            original.rename(root / 'input-unavailable')
            self.assertEqual((moved / 'code/block-1.txt').read_bytes(), LITERAL_CODE.encode('utf-8'))
            for relative in manifest['pdf_preview']['pages']:
                with Image.open(moved / relative) as image:
                    self.assertEqual(image.format, 'PNG')
                    self.assertLessEqual(max(image.size), 1600)
                    image.load()

    @unittest.skipUnless(shutil.which('pdftotext'), 'Readable output check requires Poppler pdftotext')
    def test_moved_export_rebuilds_without_original_files(self):
        from simple_ar.literature.bibtex import papers_to_bibtex
        from simple_ar.literature.models import Paper

        material_id = 'material-' + 'résumé/文献'.encode().hex()
        for case, key, authors, year in (
            ('legacy', 'p0', [], None),
            ('complete', material_id, ['René Example'], '2024'),
            ('missing-authors', material_id, [], '2024'),
            ('missing-year', material_id, ['René Example'], None),
            ('missing-both', material_id, [], None),
        ):
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    original = root / "input"
                    original.mkdir()
                    (original / "plot.svg").write_text(
                        '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="160" viewBox="0 0 400 160">'
                        '<rect width="400" height="160" fill="white"/>'
                        '<text x="20" y="50" font-size="20">Portability fixture, not research data</text>'
                        '<path d="M20 130 L360 70" stroke="blue" fill="none"/></svg>', encoding="utf-8")
                    (original / "report_body.md").write_text(
                        '# Portable export α check\n\n## Abstract\n\nRenderer integration fixture, β ≤ Γ [@' + key + '].\n\n'
                        '## Result\n\nFixture citation [@' + key + ']. Inline math \\(1-\\alpha\\). Bare α, ∞ and 3 µm; `δ_parameter`.\n\n'
                        '![Fixture plot](plot.svg)\n\n[External evidence](../measurements.json).\n'
                        + TECHNICAL_BODY, encoding="utf-8")
                    original_body = (original / 'report_body.md').read_bytes()
                    url = 'https://example.org/paper.pdf?part=a_b&view=2#results'
                    paper = Paper(key, url if case == 'missing-both' else 'Métadonnées & 50% #1',
                        authors, '', url, published=year, source='supporting_material',
                        bibliographic_notes=['Source snapshot; publication identity was not independently verified.'])
                    bibliography = ('@misc{p0, title={Explicitly incomplete metadata fixture}}\n'
                        if case == 'legacy' else papers_to_bibtex([paper]))
                    (original / "references.bib").write_text(bibliography, encoding="utf-8")
                    (original / 'figures').mkdir()
                    (original / 'figures/figures_manifest.json').write_text(json.dumps({'figures': [
                        {'path': 'plot.svg', 'caption': 'Superseded recorded plot',
                            'exports': {'svg': 'plot.svg'}}]}), encoding='utf-8')
                    exported = root / "export"
                    # The old managed entry point keeps its default; direct
                    # canonical publication explicitly owns its revised caption.
                    options = {} if case == 'legacy' else {'caption_owner': 'markdown'}
                    latex_documents = []
                    def render(argv, **kwargs):
                        if '--from=json' in argv and '--to=latex' in argv:
                            latex_documents.append(json.loads(kwargs['text']))
                        return _run(argv, **kwargs)
                    manifest = export_acm_report(original, exported, compile_pdf=True, runner=render, **options)
                    self.assertEqual(len(latex_documents), 2)
                    metadata = latex_documents[-1]['meta']
                    self.assertEqual(metadata['title']['t'], 'MetaInlines')
                    self.assertEqual(metadata['abstract']['t'], 'MetaBlocks')
                    self.assertTrue(any(node.get('t') == 'Cite' for node in _nodes(metadata['abstract'])))
                    expected_caption = 'Superseded recorded plot' if case == 'legacy' else 'Fixture plot'
                    other_caption = 'Fixture plot' if case == 'legacy' else 'Superseded recorded plot'
                    self.assertEqual(manifest['figure_caption_owner'], 'manifest' if case == 'legacy' else 'markdown')
                    self.assertEqual(manifest['bound_figure_captions'], ['plot.svg'] if case == 'legacy' else [])
                    self.assertTrue(manifest["compiled"], manifest)
                    self.assertFalse(any('Overfull \\hbox' in row for row in manifest['compile_warnings']), manifest)
                    self.assertFalse(any("undefined citation" in row.lower() or ("citation `" + key + "'") in row.lower()
                                         for row in manifest["compile_warnings"]), manifest)
                    initial_text = subprocess.check_output(
                        ['pdftotext', '-layout', str(exported / 'main.pdf'), '-'], text=True)
                    self.assertIn(INLINE_COMMAND, re.sub(r'\s+', ' ', initial_text))
                    self.assertIn('Fixturecitation[1]', ''.join(initial_text.split()))
                    self.assertNotIn('[?]', ''.join(initial_text.split()))
                    self.assertIn(''.join(expected_caption.split()), ''.join(initial_text.split()))
                    self.assertNotIn(''.join(other_caption.split()), ''.join(initial_text.split()))
                    self.assertEqual((original / "references.bib").read_text(), bibliography)
                    self.assertEqual((original / 'report_body.md').read_bytes(), original_body)
                    moved = root / "moved"
                    moved.mkdir()
                    # Copy only delivered editable inputs: no .aux/.bbl/PDF or wrapper
                    # repairs can make this ordinary rebuild appear to work.
                    for name in manifest['public_artifacts']:
                        if name == 'main.pdf':
                            continue
                        target = moved / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(exported / name, target)
                    self.assertFalse((moved / 'publication_view.json').exists())
                    self.assertFalse((moved / 'main.aux').exists())
                    original.rename(root / "input-unavailable")
                    latex = ['pdflatex', '-no-shell-escape', '-halt-on-error',
                             '-interaction=nonstopmode', 'main.tex']
                    for command in (latex, ['bibtex', 'main'], latex, latex):
                        completed = subprocess.run(command, cwd=moved, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
                        self.assertEqual(completed.returncode, 0, completed.stdout)
                    log = (moved / 'main.log').read_text(encoding='utf-8', errors='replace')
                    self.assertNotIn('undefined citations', log.lower())
                    self.assertNotIn("Citation `" + key + "'", log)
                    self.assertNotIn('Overfull \\hbox', log)
                    self.assertIn('\\bibcite{' + key + '}', (moved / 'main.aux').read_text())
                    self.assertEqual((moved / 'references.bib').read_text(), bibliography)
                    ast = json.loads(subprocess.check_output(
                        ["pandoc", "--preserve-tabs", "--from=markdown", "--to=json", str(moved / "source.md")], text=True))
                    images = [row["c"][-1][0] for row in _nodes(ast) if row.get("t") == "Image"]
                    self.assertTrue(images)
                    self.assertTrue(all((moved / name).is_file() for name in images))
                    self.assertTrue((moved / "figures/figure-1.svg").is_file())
                    variant = manifest['figure_exports']['plot.svg']['svg']
                    self.assertTrue((moved / variant).is_file())
                    source_text = ' '.join(str(row['c']) for row in _nodes(ast) if row.get('t') == 'Str')
                    self.assertIn(expected_caption, source_text)
                    self.assertNotIn(other_caption, source_text)
                    self.assertEqual((moved / 'code/block-1.txt').read_bytes(), LITERAL_CODE.encode('utf-8'))
                    self.assertEqual([row['c'][1] for row in _nodes(ast) if row.get('t') == 'CodeBlock'], [LITERAL_CODE])
                    extracted = subprocess.check_output(['pdftotext', '-layout', str(moved / 'main.pdf'), '-'], text=True)
                    readable = ''.join(extracted.split())
                    self.assertIn('Fixturecitation[1]', readable)
                    self.assertNotIn('[?]', readable)
                    self.assertIn(''.join(expected_caption.split()), readable)
                    self.assertNotIn(''.join(other_caption.split()), readable)
                    self.assertNotIn(']]' + key, readable)
                    for literal in (IDENTIFIER, LONG_PATH, r'\end{Verbatim}\input{missing-file}', '%$&#_^~{}'):
                        self.assertIn(literal, readable)
                    self.assertEqual(manifest["external_source_references"], ["../measurements.json"])


@unittest.skipUnless(os.environ.get('SAR_TEST_EXPORT_INTEGRATION') == '1' and
    all(shutil.which(name) for name in ('pandoc', 'rsvg-convert', 'pdflatex', 'bibtex',
        'pdfinfo', 'pdftotext', 'pdftoppm')),
    'Opt-in article integration requires Pandoc, librsvg, pdfLaTeX, BibTeX/urlbst and Poppler')
class GeneralArticleIntegrationTests(unittest.TestCase):
    def test_public_english_article_compiles_with_pdftex_and_moves_with_sources(self):
        self._assert_portable_article(unicode_text=False)

    @unittest.skipUnless(shutil.which('xelatex'), 'Optional Chinese case requires XeLaTeX/CTeX/Fandol')
    def test_public_bilingual_article_compiles_with_xetex_and_moves_with_sources(self):
        self._assert_portable_article(unicode_text=True)

    def _assert_portable_article(self, *, unicode_text):
        from types import SimpleNamespace
        from simple_ar.core.budget import BudgetLedger
        from simple_ar.tools.publication import PublicationToolGateway, PUBLICATION_ASSET_PROTOCOL
        from simple_ar.tools.workspace import WorkspaceToolGateway

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native = root / 'task'
            native.mkdir()
            ledger = BudgetLedger({'process_invocations': 16, 'process_wall_seconds': 1000},
                storage_path=native / 'budget.json')
            workspace = WorkspaceToolGateway(native, {}, ledger)
            source = workspace.resolve('draft.md')
            engine = 'xelatex' if unicode_text else 'pdflatex'
            title = '普通报告 / Readable report' if unicode_text else 'Readable report'
            observation = '中文说明与可编辑图表。' if unicode_text else 'Western text: résumé — cost €; scientific symbols α ≤ β.'
            body = ('---\nignored: 中文\n---\n' + f'# {title}\n\n## Summary\n\n{observation} '
                'Narrative citation @p1; evidence [@p1].\n\n'
                r'Formula: $\alpha \leq \beta$ and $E=mc^2$.' + '\n\n'
                '## Results\n\n| Measure | Value |\n|:--|--:|\n| Score | 3 |\n\n'
                '![Chart caption with evidence [@p1].](图.svg)\n' + TECHNICAL_BODY)
            source.write_text(body, encoding='utf-8')
            bibliography = '@misc{p1, title={Reader evidence}, url={https://example.org/evidence}}\n'
            source.with_name('references.bib').write_text(bibliography, encoding='utf-8')
            source.with_name('图.svg').write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="80">'
                '<rect width="160" height="80" fill="white"/><path d="M10 60L60 20L140 40" '
                'stroke="black" fill="none"/></svg>', encoding='utf-8')
            originals = {path: path.read_bytes() for path in source.parent.iterdir() if path.is_file()}
            gateway = PublicationToolGateway(native, workspace, ledger, owned_execution=True,
                asset_protocol=PUBLICATION_ASSET_PROTOCOL)
            arguments = {'markdown': 'draft.md', 'layout': 'article', 'compile_pdf': True,
                'render_preview': True, 'checks': {'max_pages': 12},
                'delivery': {'summary': 'Readable PDF and editable project', 'artifacts': []}}
            context = SimpleNamespace(attempt=SimpleNamespace(attempt_id='article'))
            binding = gateway.approval_binding(arguments, identity='article')
            self.assertIn('schema_version', binding['article_rendering'])
            self.assertNotIn('engine', binding['article_rendering'])  # Selection uses the authorized Pandoc pass.
            denied = gateway.execute_tool('export_report', context, {**arguments, 'layout': 'acm'},
                approval={'binding': binding})
            self.assertEqual(denied.status, 'blocked')
            self.assertEqual(ledger.entries, ())
            result = gateway.execute_tool('export_report', context, arguments, approval={'binding': binding})
            self.assertTrue(result.ok, result.error)
            manifest = result.data['manifest']
            self.assertTrue(manifest['compiled'], manifest)
            self.assertEqual(manifest['article_rendering']['schema_version'], binding['article_rendering']['schema_version'])
            self.assertEqual(manifest['article_rendering']['engine'], engine)
            self.assertEqual(manifest['checks']['status'], 'passed')
            self.assertTrue(manifest['pdf_preview']['rendered'])
            self.assertIn('delivery', result.data)
            self.assertFalse(any('Missing character:' in warning or 'undefined citation' in warning.lower()
                for warning in manifest['compile_warnings']), manifest)
            project = workspace.resolve(result.data['project'])
            self.assertEqual((project / 'references.bib').read_bytes(), bibliography.encode('utf-8'))
            self.assertEqual({path: path.read_bytes() for path in originals}, originals)
            self.assertEqual((project / 'code/block-1.txt').read_bytes(), LITERAL_CODE.encode('utf-8'))
            receipts = [json.loads(path.read_text(encoding='utf-8'))
                for path in (native / 'tools/processes').glob('*/invocation.json')]
            # The original runner owns every physical invocation and settlement.
            self.assertEqual(len(receipts), len(ledger.entries))
            self.assertTrue(all(entry.status == 'settled' and entry.purpose == 'local_process'
                for entry in ledger.entries))
            charged = ledger.to_dict()
            self.assertEqual(PublicationToolGateway(native, workspace, ledger, owned_execution=True,
                asset_protocol=PUBLICATION_ASSET_PROTOCOL).call('export_report', arguments, identity='article').data,
                result.data)
            self.assertEqual(ledger.to_dict(), charged)
            moved = root / 'portable'
            moved.mkdir()
            for relative in manifest['public_artifacts']:
                target = moved / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(project / relative, target)
            workspace.workspace.rename(root / 'authoring-unavailable')
            rebuilt = _compile(moved, True, engine=engine, fail_on_missing_glyphs=True)
            self.assertTrue(rebuilt['compiled'], rebuilt)
            text = subprocess.check_output(['pdftotext', '-layout', str(moved / 'main.pdf'), '-'], text=True)
            compact = ''.join(text.split())
            self.assertIn(''.join(title.split()), compact)
            self.assertIn('中文说明与可编辑图表' if unicode_text else 'Westerntext:résumé', compact)
            self.assertIn('Summary', text)
            self.assertIn('Narrativecitation[1]', compact)
            self.assertNotIn('[?]', compact)
            self.assertNotIn('p1', compact)
            self.assertTrue((moved / 'figures/figure-1.svg').is_file())
            self.assertIn(f'Selected engine: {engine}', (moved / 'README.txt').read_text(encoding='utf-8'))
            if not unicode_text:
                self.assertNotIn('ctex', (moved / 'main.tex').read_text(encoding='utf-8'))
            qa_root = os.environ.get('SAR_TEST_EXPORT_QA_DIR')
            if qa_root:
                Path(qa_root).mkdir(parents=True, exist_ok=True)
                retained = Path(tempfile.mkdtemp(prefix='article-', dir=qa_root))
                shutil.copytree(root / 'authoring-unavailable' / project.relative_to(workspace.workspace), retained / 'export')
                shutil.copytree(moved, retained / 'rebuilt')
                print('Article PDF/preview evidence:', retained, flush=True)


if __name__ == "__main__":
    unittest.main()
