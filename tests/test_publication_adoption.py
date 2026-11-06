"""External PDF provenance via real process receipts; no TeX or paid calls.

The tiny PDF-like bytes deliberately make no quality claim. These scenarios
exercise publication admission only; independent content review remains separate.
"""
import hashlib
import json
import sys
import unittest

from simple_ar.tools.publication import PublicationToolGateway
from tests import test_professional_work_loop as fixture


class PublicationAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.OperationsWorkLoopTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case._create(current_method=True, delivery_protocol=None,
            selection_policy='native.v3', professional_contract='work.v12')
        self.case._write('report.md', '# Observations\n\n![Recorded](figure.svg)\n')
        self.case._write('figure.svg', '<svg xmlns="http://www.w3.org/2000/svg"/>')
        self.case._write('references.bib', '@misc{observation,title={Observation}}')
        self.case._write('make.py', "from pathlib import Path\nPath('paper.pdf').write_bytes(b'%PDF-1.4\\nfixture\\n')\n")

    def publish(self, inputs=None, **options):
        self.case._submit(fixture._action('run_process', argv=[sys.executable, 'make.py'],
            inputs=inputs if inputs is not None else ['make.py', 'report.md', 'figure.svg', 'references.bib'],
            outputs=['paper.pdf'], **options), approve=lambda *_: True)
        return self.case._ok('run_process')

    def check(self, lifecycle='integrated.v1', formats=None):
        owner = PublicationToolGateway(self.case.root, self.case.app.workspace, self.case.app.ledger,
            work_lifecycle=lifecycle)
        return owner.check_requested_delivery([], ['report.md', 'paper.pdf'],
            formats=formats or ['markdown', 'pdf'], manuscript='report.md')

    def test_explicit_process_inputs_are_consumed_after_reload_without_new_state(self):
        self.publish()
        before = (self.case.root / 'budget_ledger.json').read_bytes()
        self.case._reload()
        self.assertEqual(self.check(), {self.case.app.workspace.resolve('paper.pdf')})
        self.assertEqual((self.case.root / 'budget_ledger.json').read_bytes(), before)
        self.assertFalse((self.case.root / 'delivery.json').exists())
        with self.assertRaises(ValueError):
            self.check(lifecycle=None)

    def test_multistage_publication_consumes_the_original_manuscript_and_assets(self):
        self.case._write('make_tex.py', "from pathlib import Path\nPath('body.tex').write_text('intermediate')\n")
        self.case._submit(fixture._action('run_process', argv=[sys.executable, 'make_tex.py'],
            inputs=['make_tex.py', 'report.md', 'figure.svg', 'references.bib'], outputs=['body.tex']),
            approve=lambda *_: True)
        self.case._ok('run_process')
        self.publish(inputs=['make.py', 'body.tex'])
        self.assertEqual(self.check(), {self.case.app.workspace.resolve('paper.pdf')})

    def test_pdf_presence_and_same_name_never_supply_missing_source_edges(self):
        self.publish(inputs=['make.py'])
        with self.assertRaisesRegex(ValueError, 'explicitly consume'):
            self.check()

    def test_process_bound_plain_text_with_pdf_suffix_is_rejected(self):
        self.case.app.workspace.resolve('make.py').write_text(
            "from pathlib import Path\nPath('paper.pdf').write_text('plain text, no PDF container')\n",
            encoding='utf-8')
        self.publish()
        with self.assertRaisesRegex(ValueError, 'no PDF header'):
            self.check()

    def test_undeclared_pdf_production_is_not_retroactively_bound(self):
        self.case._submit(fixture._action('run_process', argv=[sys.executable, 'make.py']),
            approve=lambda *_: True)
        self.case._ok('run_process')
        with self.assertRaisesRegex(ValueError, 'version-bound producer'):
            self.check()

    def test_primitive_file_write_is_not_a_process_publication(self):
        self.case._write('paper.pdf', '%PDF-1.4\nordinary authored bytes\n')
        with self.assertRaisesRegex(ValueError, 'actual process producer'):
            self.check()

    def test_replaced_markdown_or_figure_invalidates_the_pdf(self):
        self.publish()
        for name in ('report.md', 'figure.svg'):
            with self.subTest(name=name):
                path = self.case.app.workspace.resolve(name)
                original = path.read_bytes()
                path.write_bytes(original + b'\nchanged')
                with self.assertRaises(ValueError):
                    self.check()
                path.write_bytes(original)

    def test_new_bibliography_must_have_been_consumed(self):
        self.publish(inputs=['make.py', 'report.md', 'figure.svg'])
        with self.assertRaisesRegex(ValueError, 'explicitly consume'):
            self.check()

    def test_changed_script_is_a_stale_declared_dependency(self):
        self.publish()
        self.case.app.workspace.resolve('make.py').write_text('# revised\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'inputs changed'):
            self.check()

    def test_incomplete_or_forged_process_receipt_cannot_be_adopted(self):
        result = self.publish()
        path = self.case.root / result['data']['receipt']
        original = path.read_bytes()
        for mutation in ('output_binding', 'returncode'):
            with self.subTest(mutation=mutation):
                record = json.loads(original)
                if mutation == 'output_binding':
                    record.pop('output_binding')
                else:
                    record['returncode'] = 5
                path.write_text(json.dumps(record), encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.check()
                path.write_bytes(original)

    def test_unrelated_undeclared_process_does_not_erase_accepted_content_version(self):
        self.publish()
        self.case._submit(fixture._action('run_process', argv=[sys.executable, '-c', 'pass']),
            approve=lambda *_: True)
        self.case._ok('run_process')
        self.assertEqual(self.check(), {self.case.app.workspace.resolve('paper.pdf')})

    def test_failed_later_writer_is_a_barrier_even_when_pdf_bytes_match(self):
        self.publish()
        before = hashlib.sha256(self.case.app.workspace.resolve('paper.pdf').read_bytes()).hexdigest()
        self.case._submit(fixture._action('run_process', argv=[sys.executable, '-c', 'raise SystemExit(2)'],
            inputs=['make.py', 'report.md', 'figure.svg', 'references.bib'], outputs=['paper.pdf']),
            approve=lambda *_: True)
        self.assertEqual(self.case._last('run_process')['status'], 'error')
        self.assertEqual(hashlib.sha256(self.case.app.workspace.resolve('paper.pdf').read_bytes()).hexdigest(), before)
        with self.assertRaises(ValueError):
            self.check()

    def test_external_pdf_cannot_claim_an_editable_tex_project(self):
        self.publish()
        with self.assertRaises(ValueError):
            self.check(formats=['markdown', 'pdf', 'tex'])

    def test_markdown_with_unrelated_pdf_has_no_publication_obligation(self):
        self.case._write('paper.pdf', '%PDF unrelated attachment')
        self.assertEqual(self.check(formats=['markdown']), set())

    def test_only_verified_pdf_versions_are_identified_as_current_publication(self):
        self.publish()
        digest = self.case._read('paper.pdf')['sha256']
        self.case._submit(fixture._action('copy_file', source='paper.pdf', source_sha256=digest,
            path='old.pdf'))
        self.case._ok('copy_file')
        source = self.case.app.workspace.resolve('report.md')
        version = self.case._read('report.md')['sha256']
        self.case._submit(fixture._action('write_file', path='report.md', expected_sha256=version,
            content=source.read_text(encoding='utf-8') + '\nCurrent revision.\n'))
        self.case._ok('write_file')
        self.publish()
        self.case._write('attachment.pdf', '%PDF unrelated attachment')
        owner = PublicationToolGateway(self.case.root, self.case.app.workspace, self.case.app.ledger,
            work_lifecycle='integrated.v1')
        verified = owner.check_requested_delivery([], ['report.md', 'old.pdf', 'paper.pdf', 'attachment.pdf'],
            formats=['markdown', 'pdf'], manuscript='report.md')
        self.assertEqual(verified, {self.case.app.workspace.resolve('paper.pdf')})
