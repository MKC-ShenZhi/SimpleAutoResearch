"""Opt-in real renderer/TeX check; no model, network or dependency installation."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from simple_ar.report.export import export_acm_report, _nodes


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
    @unittest.skipUnless(all(shutil.which(name) for name in ('pdfinfo', 'pdftoppm')),
        'PDF preview integration requires Poppler')
    def test_preview_of_compiled_canonical_body_moves_with_project(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / 'input'
            original.mkdir()
            body = '# Canonical body\n\n## Result ' + IDENTIFIER + '\n\nBounded renderer fixture.\n' + TECHNICAL_BODY
            (original / 'report_body.md').write_text(body, encoding='utf-8')
            output = root / 'export'
            manifest = export_acm_report(original, output,
                title='A descriptive title with ordinary spaces and a technical identifier ' + IDENTIFIER,
                compile_pdf=True, render_preview=True)
            self.assertEqual(manifest['status'], 'compiled', manifest)
            self.assertTrue(manifest['compiled'])
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
                        '# Portable export α check\n\n## Abstract\n\nRenderer integration fixture, β ≤ Γ.\n\n'
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
                    manifest = export_acm_report(original, exported, compile_pdf=True, **options)
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
                    for name in ('main.tex', 'body.tex', 'references.bib', 'source.md', 'README.txt'):
                        shutil.copyfile(exported / name, moved / name)
                    for name in ('figures', 'code'):
                        shutil.copytree(exported / name, moved / name)
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


if __name__ == "__main__":
    unittest.main()
