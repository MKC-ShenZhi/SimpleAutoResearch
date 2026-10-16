"""Supplied PDFs must not vanish when remote full-text fetching is off."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import redirect_stdout
import io
import tempfile
import unittest

from simple_ar.app.research_application import ResearchApplication
from simple_ar.app.tool_application import ToolApplication
from simple_ar.literature.models import Paper
from simple_ar.research.documents.fulltext import build_fulltext_manifest
from simple_ar.research.documents.ingest import build_document_bundle
from simple_ar.research.documents.ingest import DocumentBundle
from simple_ar.research.documents.records import build_document_records
from simple_ar.research.contracts import DocumentRecord, TextChunk
from simple_ar.research.evidence.reader import ReadRequest, ReadResult, read_documents
from simple_ar.research.sources.base import build_source_plan


class SuppliedPdfIngestTest(unittest.TestCase):
    def test_fixed_supplied_pool_goes_directly_to_model_reading_unless_screening_is_explicit(self) -> None:
        records = [DocumentRecord(name, name, "local_files") for name in ("a", "b")]
        bundle = DocumentBundle(records, {}, {}, [], [TextChunk(name, name, "Source body.") for name in ("a", "b")])
        for mode in ("auto", "llm"):
            with self.subTest(mode=mode), patch(
                "simple_ar.research.evidence.reader.screen_papers_with_llm", return_value=[],
            ) as screen, patch(
                "simple_ar.research.evidence.reader.read_paper_notes_with_llm", return_value=[],
            ) as reader:
                result = read_documents(ReadRequest(bundle=bundle, required_document_ids=("a", "b"),
                    use_llm=True, llm_client=object(), config={"read_screening": mode}))
                self.assertEqual([row.document_id for row in result.bundle.records], ["a", "b"])
                self.assertEqual([row["paper_id"] for row in result.screening_decisions], ["a", "b"])
                self.assertEqual([row["document_id"] for row in reader.call_args.kwargs["papers"]], ["a", "b"])
                self.assertEqual(screen.call_count, int(mode == "llm"))

    def test_llm_screening_keeps_supplied_paper_within_shortlist_limit(self) -> None:
        records = [
            DocumentRecord(
                document_id=document_id, title=document_id, source=source,
                source_id=document_id, abstract="Evidence about the method.",
            )
            for document_id, source in (
                ("supplied", "local_files"), ("retrieved-a", "arxiv"),
                ("retrieved-b", "openalex"),
            )
        ]
        bundle = DocumentBundle(
            records=records, fulltext_manifest={}, fulltext_extraction={}, sections=[],
            chunks=[
                TextChunk(chunk_id=f"{record.document_id}#chunk-001",
                          document_id=record.document_id, text=record.abstract)
                for record in records
            ],
        )
        decisions = [
            {"paper_id": "supplied", "decision": "drop", "reading_priority": 3},
            {"paper_id": "retrieved-a", "decision": "keep", "reading_priority": 1},
            {"paper_id": "retrieved-b", "decision": "keep", "reading_priority": 2},
        ]
        with patch(
            "simple_ar.research.evidence.reader.screen_papers_with_llm",
            return_value=decisions,
        ), patch(
            "simple_ar.research.evidence.reader.read_paper_notes_with_llm",
            return_value=[],
        ):
            result = read_documents(ReadRequest(
                bundle=bundle, required_document_ids=("supplied",),
                use_llm=True, llm_client=object(),
                config={"read_screening_max_shortlist": 2},
            ))
        self.assertEqual(
            [record.document_id for record in result.bundle.records],
            ["supplied", "retrieved-a"],
        )
        self.assertEqual(len(result.bundle.chunks), 2)
        self.assertEqual(len(result.screening_decisions), 3)
        self.assertEqual(result.screening_decisions[0]["decision"], "keep")
        self.assertEqual(result.screening_decisions[2]["decision"], "drop")

    def test_supplied_documents_over_shortlist_limit_fail_explicitly(self) -> None:
        records = [
            DocumentRecord(document_id=name, title=name, source="local_files", source_id=name)
            for name in ("a", "b")
        ]
        bundle = DocumentBundle(
            records=records, fulltext_manifest={}, fulltext_extraction={}, sections=[], chunks=[],
        )
        with patch(
            "simple_ar.research.evidence.reader.screen_papers_with_llm",
            return_value=[{"paper_id": "a", "decision": "keep"}],
        ):
            with self.assertRaisesRegex(ValueError, "supplied documents exceed"):
                read_documents(ReadRequest(
                    bundle=bundle, required_document_ids=("a", "b"),
                    use_llm=True, llm_client=object(),
                    config={"read_screening_max_shortlist": 1},
                ))

    def test_configured_pdf_reaches_normal_session_reading(self) -> None:
        from simple_ar.cli.main import main
        from simple_ar.cli.start import session_materials

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "method.pdf").write_bytes(b"%PDF-1.7\nfixture")
            config = root / "research.toml"
            config.write_text(
                '[task]\ngoal="Summarize the supplied TabM method"\noutputs=["summary"]\noutput_root="out"\n'
                '[model]\nname=""\n[research]\nproviders=["local_files"]\n'
                'use_fulltext=false\nallow_pdf_download=false\nmax_pdf_pages=5\n'
                '[assets]\npapers=["method.pdf"]\n', encoding="utf-8",
            )
            console = io.StringIO()
            with patch(
                "simple_ar.research.documents.extractors._read_pdf",
                return_value="# Method\n\nTabM uses parameter-efficient ensembling.",
            ) as parse_pdf, redirect_stdout(console):
                try:
                    main(["research-session", "--config", str(config)])
                except SystemExit as exc:
                    self.fail(f"Session stopped: {exc}; console: {console.getvalue()}")
            parse_pdf.assert_called_once()
            self.assertEqual(parse_pdf.call_args.kwargs["max_pages"], 5)
            session = next((root / "out").iterdir())
            app = ToolApplication.load(session)
            self.assertEqual(app.session.manifest.status, "completed")
            accepted = [app.session.store.read_json(tool['result'])
                for turn in app.state['turns'] for tool in turn['tools']]
            research = [row for row in accepted if row['tool_name'] in {'research', 'research_collect'}
                        and row['status'] == 'ok' and row['data'].get('operation') == 'summary']
            self.assertEqual(len(research), 1)
            result = research[0]
            self.assertEqual(result['data']['operation'], 'summary')
            delivered = session_materials(session)['summary']
            self.assertCountEqual(delivered, [app.workspace.resolve(path) for path in result['artifacts']])
            bundle_path = app.workspace.resolve(result['data']['document_record'])
            self.assertIn(bundle_path, delivered)
            bundle = DocumentBundle.from_handoff_dict(app.session.store.read_json(
                bundle_path.relative_to(session)))
            self.assertEqual(len(bundle.records), 1)
            self.assertEqual(bundle.records[0].extraction_status, 'parsed')
            self.assertTrue(bundle.fulltext_manifest['enabled'])
            self.assertFalse(bundle.fulltext_manifest['allow_pdf_download'])
            self.assertTrue(any(
                "parameter-efficient ensembling" in section.text
                for section in bundle.sections
            ))
            self.assertTrue(any('parameter-efficient ensembling' in chunk.text for chunk in bundle.chunks))
            # Inspect only artifacts from the accepted receipt; historical
            # attempts and the legacy state-ref layout do not own this result.
            handoffs = [app.session.store.read_json(path.relative_to(session))
                for path in delivered if path.suffix == '.json']
            reads = [row for row in handoffs if row.get('schema_version') == 'read_result.v1']
            self.assertEqual(len(reads), 1)
            reading = ReadResult.from_handoff_dict(reads[0], bundle=bundle)
            self.assertEqual(reading.status, 'completed')
            self.assertEqual(len(reading.paper_cards), 1)
            self.assertEqual({row['document_id'] for row in reads[0]['documents']},
                             {bundle.records[0].document_id})
            self.assertEqual({row['chunk_id'] for row in reads[0]['source_spans']},
                             {chunk.chunk_id for chunk in bundle.chunks})

    def test_application_passes_local_pdf_to_source_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paper = Path(directory) / "method.pdf"
            paper.write_bytes(b"%PDF-1.7\nfixture")
            application = SimpleNamespace(assets=(SimpleNamespace(
                locator=str(paper), role="paper", availability="available",
            ),))
            self.assertEqual(ResearchApplication._local_documents(application), (paper,))

    def test_local_pdf_is_read_without_remote_fulltext_permission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = root / "method.pdf"
            paper.write_bytes(b"%PDF-1.7\nfixture")
            plan = build_source_plan(
                topic="TabM method",
                problem_markdown="",
                config={
                    "research_sources": ["local_files"],
                    "research_local_documents": [str(paper)],
                    "research_use_fulltext": False,
                    "research_allow_pdf_download": False,
                },
                default_query="TabM method",
                default_max_results=3,
            )
            with patch(
                "simple_ar.research.documents.extractors._read_pdf",
                return_value="# Method\n\nThe paper uses parameter-efficient ensembling.",
            ) as parse_pdf:
                bundle = build_document_bundle(
                    papers=[], source_plan=plan, cache_dir=None,
                    extraction_dir=root / "extracted",
                )
            parse_pdf.assert_called_once()
            self.assertTrue(bundle.fulltext_manifest["enabled"])
            self.assertFalse(bundle.fulltext_manifest["allow_pdf_download"])
            self.assertEqual(bundle.records[0].extraction_status, "parsed")
            self.assertIn("parameter-efficient ensembling", bundle.sections[0].text)
            self.assertTrue(bundle.chunks)

    def test_supplied_pdf_does_not_spend_remote_fetch_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paper = Path(directory) / "method.pdf"
            paper.write_bytes(b"%PDF-1.7\nfixture")
            plan = build_source_plan(
                topic="TabM method", problem_markdown="",
                config={
                    "research_sources": ["fixture"],
                    "research_local_documents": [str(paper)],
                    "research_use_fulltext": True,
                    "research_max_fulltext_documents": 1,
                    "research_max_fulltext_fetch_attempts": 1,
                },
                default_query="TabM method", default_max_results=3,
            )
            remote = Paper(
                id="remote-1", title="Related method", authors=[], abstract="Related.",
                url="https://example.test/method.html", source="fixture",
            )
            records = build_document_records(papers=[remote], source_plan=plan)
            manifest = build_fulltext_manifest(records=records, source_plan=plan)
            self.assertEqual(manifest["documents"][0]["hints"][0]["status"], "cached")
            self.assertEqual(manifest["documents"][1]["hints"][0]["status"], "selected")
            self.assertEqual(manifest["fetch_attempt_count"], 1)


if __name__ == "__main__":
    unittest.main()
