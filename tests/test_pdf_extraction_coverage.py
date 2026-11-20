from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from simple_ar.core.capabilities import ArtifactRef
from simple_ar.research.contracts import SourcePlan
from simple_ar.research.documents.extractors import LocalDocumentParser, _read_pdf
from simple_ar.research.documents.ingest import build_local_document_bundle, DocumentBundle
from simple_ar.research.evidence.reader import ReadRequest, read_documents
from simple_ar.report.projection import build_material_report_inputs, attach_report_read_evidence


class PDFExtractionCoverageTests(unittest.TestCase):
    def test_default_is_unlimited_and_explicit_positive_limit_remains_supported(self):
        self.assertIsNone(LocalDocumentParser().max_pdf_pages)
        self.assertIsNone(LocalDocumentParser.from_source_plan(SourcePlan(queries=[])).max_pdf_pages)
        self.assertEqual(LocalDocumentParser(max_pdf_pages=5).max_pdf_pages, 5)
        for value in (0, -1, True):
            with self.assertRaises(ValueError):
                LocalDocumentParser(max_pdf_pages=value)

    def pdf(self, folder):
        from pypdf import PdfWriter
        path = Path(folder) / 'long.pdf'
        writer = PdfWriter()
        for _ in range(23):
            writer.add_blank_page(width=120, height=120)
        writer.write(path)
        return path

    def test_actual_page_coverage_and_empty_pages_are_not_hidden(self):
        with tempfile.TemporaryDirectory() as folder:
            pdf = self.pdf(folder)
            parsed = LocalDocumentParser().parse(pdf)
            self.assertEqual(parsed.coverage['total_pages'], 23)
            self.assertEqual(parsed.coverage['extracted_pages'], 23)
            self.assertFalse(parsed.coverage['truncated'])
            self.assertIsNone(parsed.coverage['page_limit'])
            self.assertEqual(parsed.coverage['empty_text_pages'], list(range(1, 24)))
            limited = LocalDocumentParser(max_pdf_pages=3).parse(pdf)
            self.assertTrue(limited.coverage['truncated'])
            self.assertEqual(limited.coverage['extracted_pages'], 3)

    def test_coverage_survives_ingest_restore_reading_and_material_writing(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            pdf = self.pdf(folder)
            before = pdf.read_bytes()
            # Page transport is real; mock only the blank fixture's text so
            # ingest has a readable body. The previous test checks actual pages.
            def extraction(path, *, max_pages, coverage):
                _read_pdf(path, max_pages=max_pages, coverage=coverage)
                coverage['empty_text_pages'] = []
                return ('Abstract\nA supplied summary.\nMethods\nObserved source text. '
                        'Symbol \ud835\udc00; malformed \ud835. Ligature a\x00b\x01c.\n')
            with patch('simple_ar.research.documents.extractors._read_pdf', side_effect=extraction):
                bundle = build_local_document_bundle([pdf], extraction_dir=Path(folder)/'extracted',
                                                    parser=LocalDocumentParser(max_pdf_pages=3))
            restored = DocumentBundle.from_handoff_dict(bundle.to_handoff_dict())
            coverage = restored.records[0].metadata['fulltext_extraction']['coverage']
            self.assertEqual(coverage['extracted_pages'], 3)
            self.assertEqual(coverage['unpaired_unicode_replacements'], 1)
            self.assertEqual(coverage['escaped_control_characters'], 2)
            text = Path(restored.records[0].local_path).read_text(encoding='utf-8')
            self.assertIn('\U0001d400', text)
            self.assertIn('malformed \ufffd', text)
            self.assertIn(r'a\u0000b\u0001c', text)
            self.assertEqual(restored.fulltext_extraction['documents'][0]['coverage'], coverage)
            read = read_documents(ReadRequest(restored))
            self.assertEqual(read.status, 'partial')
            self.assertTrue(any('3/23' in note for note in read.diagnostics))
            context, memory = build_material_report_inputs(topic='Review provided source', documents=restored,
                documents_ref=ArtifactRef('documents.json'), assets=[SimpleNamespace(locator=str(pdf), role='paper')])
            self.assertTrue(any('3/23' in note for note in memory.limitations))
            self.assertTrue(any('unpaired Unicode' in note for note in memory.limitations))
            self.assertTrue(any('control characters' in note for note in memory.limitations))
            self.assertEqual(context.source_handles[0].metadata['extraction_coverage'], coverage)
            context, memory = attach_report_read_evidence(context, memory, documents=restored,
                                                        read=read, read_ref=ArtifactRef('read.json'))
            self.assertEqual(context.source_handles[0].metadata['extraction_coverage'], coverage)
            self.assertEqual(sum('3/23' in note for note in memory.limitations), 1)
            self.assertEqual(pdf.read_bytes(), before)

    def test_two_argument_external_parser_contract_remains_compatible(self):
        from simple_ar.research.documents.ports import ParsedDocument
        parsed = ParsedDocument('External parser text.', 'external')
        self.assertEqual(parsed.coverage, {})

    def test_backend_cannot_silently_ignore_an_explicit_pdf_page_limit(self):
        with self.assertRaisesRegex(RuntimeError, 'cannot enforce max_pdf_pages'):
            LocalDocumentParser(parser_backend='unstructured', max_pdf_pages=3).parse(Path('paper.pdf'))


if __name__ == '__main__':
    unittest.main()
