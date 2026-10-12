from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from simple_ar.core import CapabilityRegistry, SessionController
from simple_ar.research.contracts import DocumentRecord, SourcePlan
from simple_ar.research.documents import (
    DocumentBundle,
    DocumentIngestRequest,
    DocumentResolution,
    LocalDocumentParser,
    ParsedDocument,
    build_local_document_bundle,
    run_document_ingest_capability,
)
from simple_ar.research.documents.extractors import apply_fulltext_extraction
from simple_ar.research.evidence.reader import (
    ReadRequest,
    read_documents,
    run_read_capability,
)


class DocumentPortTests(unittest.TestCase):
    def test_default_parser_is_reusable_without_extraction_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "paper.md"
            note.write_text("# Method\n\nA bounded method.\n", encoding="utf-8")

            parsed = LocalDocumentParser().parse(note)

            self.assertEqual(parsed.parser, "plain_text")
            self.assertIn("bounded method", parsed.text)
            import json
            notebook = {'nbformat': 4, 'cells': [
                {'cell_type': 'markdown', 'source': ['# Protocol\n', 'Use the original split.']},
                {'cell_type': 'code', 'source': 'print("not executed")\n',
                 'outputs': [{'data': {'image/png': 'binary-output' * 2000}}]}]}
            for suffix in ('.ipynb', '.txt'):
                original = Path(tmp) / ('experiment' + suffix)
                raw = json.dumps(notebook)
                original.write_text(raw, encoding='utf-8')
                view = LocalDocumentParser().parse(original)
                self.assertEqual(view.parser, 'notebook_cells')
                self.assertIn('Use the original split.', view.text)
                self.assertIn('print("not executed")\n', view.text)
                self.assertNotIn('binary-output', view.text)
                self.assertEqual(view.coverage['extracted_cells'], 2)
                self.assertEqual(view.coverage['cells_with_omitted_outputs'], 1)
                self.assertEqual(original.read_text(encoding='utf-8'), raw)
                bundle = build_local_document_bundle([original],
                    extraction_dir=Path(tmp) / ('extracted' + suffix), max_chunks=20)
                self.assertEqual(bundle.records[0].parser, 'notebook_cells')
                self.assertTrue(bundle.chunks)
                self.assertTrue(all('binary-output' not in chunk.text for chunk in bundle.chunks))
                self.assertIn('Use the original split.', '\n'.join(chunk.text for chunk in bundle.chunks))
                self.assertNotEqual(bundle.records[0].local_path, str(original))
            original.write_text('{"metric": 0.7}', encoding='utf-8')
            self.assertEqual(LocalDocumentParser().parse(original).parser, 'plain_text')
            original = Path(tmp) / 'invalid.ipynb'
            original.write_text('{"nbformat": 4, "cells": [{}]}', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'invalid_notebook_cell'):
                LocalDocumentParser().parse(original)
            pdf = Path(tmp) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.7\nfixture")
            from types import SimpleNamespace
            from unittest.mock import patch
            page = {"/Annots": [SimpleNamespace(get_object=lambda: {
                "/A": {"/S": "/URI", "/URI": "https://example.test/author-code"}})]}
            class LinkedPage(dict):
                def extract_text(self):
                    return "Code is available through the linked footnote."
            with patch.dict("sys.modules", {"pypdf": SimpleNamespace(
                    PdfReader=lambda _: SimpleNamespace(pages=[LinkedPage(page)]))}):
                parsed = LocalDocumentParser(max_pdf_pages=1).parse(pdf)
            self.assertIn("https://example.test/author-code", parsed.text)
            self.assertIn("not verified contents", parsed.text)
            self.assertEqual(parsed.coverage["empty_text_pages"], [])

    def test_local_bundle_enters_read_without_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note = root / "paper.md"
            note.write_text(
                "# Method\n\nA bounded method uses a public benchmark.\n",
                encoding="utf-8",
            )

            bundle = build_local_document_bundle(
                [note],
                extraction_dir=root / "extracted",
                max_chunks=2,
            )
            result = read_documents(ReadRequest(bundle=bundle))

            self.assertEqual(result.status, "completed")
            self.assertEqual(len(bundle.records), 1)
            self.assertEqual(bundle.records[0].parser, "plain_text")
            self.assertTrue(bundle.chunks)

    def test_custom_resolver_and_parser_are_used_by_ingest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.bin"
            source.write_bytes(b"fixture")
            record = DocumentRecord(
                document_id="doc-1",
                title="Fixture document",
                source="fixture",
                extraction_status="metadata_only",
            )
            plan = SourcePlan(
                queries=["fixture"],
                sources=["fixture"],
                require_fulltext=True,
                budget={"parser_backend": "basic"},
            )

            class AliasResolver:
                def resolve(
                    self,
                    *,
                    document_id: str,
                    local_path: str | None,
                    url: str | None,
                ) -> DocumentResolution:
                    return DocumentResolution(
                        document_id=document_id,
                        status="available",
                        path=source,
                        reason="fixture_alias",
                    )

            class FixtureParser:
                def parse(self, path: Path) -> ParsedDocument:
                    self.path = path
                    return ParsedDocument(
                        text="A parser supplied this document.",
                        parser="fixture_parser",
                    )

            parser = FixtureParser()
            updated, manifest = apply_fulltext_extraction(
                records=[record],
                fulltext_manifest={
                    "enabled": True,
                    "documents": [
                        {
                            "document_id": "doc-1",
                            "hints": [{"status": "cached", "local_path": "alias"}],
                        }
                    ],
                },
                source_plan=plan,
                extraction_dir=root / "extracted",
                resolver=AliasResolver(),
                parser=parser,
            )

            self.assertEqual(updated[0].extraction_status, "parsed")
            self.assertEqual(updated[0].parser, "fixture_parser")
            self.assertEqual(manifest["parsed_count"], 1)
            self.assertEqual(parser.path, source)

    def test_local_resolver_marks_missing_resources_without_raising(self) -> None:
        from simple_ar.research.documents.ports import LocalDocumentResolver

        result = LocalDocumentResolver().resolve(
            document_id="doc-1",
            local_path="does-not-exist.pdf",
            url=None,
        )

        self.assertEqual(result.status, "missing")
        self.assertEqual(result.reason, "cached_path_missing")

    def test_resolver_failure_is_retained_as_document_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = DocumentRecord(
                document_id="doc-1",
                title="Unavailable document",
                source="fixture",
            )
            plan = SourcePlan(
                queries=["fixture"],
                sources=["fixture"],
                require_fulltext=True,
            )

            class BrokenResolver:
                def resolve(
                    self,
                    *,
                    document_id: str,
                    local_path: str | None,
                    url: str | None,
                ) -> DocumentResolution:
                    raise RuntimeError("resolver unavailable")

            updated, manifest = apply_fulltext_extraction(
                records=[record],
                fulltext_manifest={
                    "enabled": True,
                    "documents": [
                        {
                            "document_id": "doc-1",
                            "hints": [{"status": "cached", "local_path": "alias"}],
                        }
                    ],
                },
                source_plan=plan,
                extraction_dir=Path(tmp) / "extracted",
                resolver=BrokenResolver(),
            )

            self.assertEqual(updated[0].extraction_status, "metadata_only")
            self.assertEqual(manifest["status_counts"]["failed"], 1)
            self.assertIn("resolver unavailable", manifest["documents"][0]["reason"])

    def test_document_bundle_handoff_round_trips_without_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note = root / "paper.md"
            note.write_text("# Method\n\nA bounded method.\n", encoding="utf-8")
            bundle = build_local_document_bundle(
                [note],
                extraction_dir=root / "extracted",
                max_chunks=2,
            )

            restored = DocumentBundle.from_handoff_dict(bundle.to_handoff_dict())

            self.assertEqual(
                [record.document_id for record in restored.records],
                [record.document_id for record in bundle.records],
            )
            self.assertEqual(
                [chunk.text for chunk in restored.chunks],
                [chunk.text for chunk in bundle.chunks],
            )
            self.assertEqual(restored.fulltext_extraction, bundle.fulltext_extraction)

    def test_document_ingest_capability_persists_one_bundle_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note = root / "paper.md"
            note.write_text("# Results\n\nAccuracy improves.\n", encoding="utf-8")
            registry = CapabilityRegistry()
            registry.register("document_ingest", run_document_ingest_capability)
            registry.register("read", run_read_capability)
            controller = SessionController.create(
                root / "session",
                session_id="document-ingest",
                topic="document fixture",
                profile="research_brief",
                registry=registry,
            )
            source_plan = SourcePlan(
                queries=["local documents"],
                sources=["local_files"],
                local_documents=[str(note)],
                require_fulltext=True,
            )

            result = controller.execute_attempt(
                "document_ingest",
                attempt_id="attempt-001",
                request=DocumentIngestRequest(
                    papers=(),
                    source_plan=source_plan,
                    extraction_dir=root / "extracted",
                ),
            )

            self.assertEqual(result.status, "completed")
            ref = controller.attempt_output_refs("attempt-001")[0]
            payload = controller.store.read_json(ref)
            self.assertEqual(payload["schema_version"], "document_bundle.v1")
            self.assertEqual(len(payload["documents"]), 1)

            restored = DocumentBundle.from_handoff_dict(payload)
            read_result = controller.execute_attempt(
                "read",
                attempt_id="attempt-002",
                inputs=(ref,),
                request=ReadRequest(bundle=restored),
            )
            self.assertEqual(read_result.status, "completed")


if __name__ == "__main__":
    unittest.main()
