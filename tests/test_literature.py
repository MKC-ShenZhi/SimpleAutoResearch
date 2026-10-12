from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from simple_ar.literature.arxiv_client import _TimeoutSession, _paper_from_result, is_rate_limit_message
from simple_ar.literature.bibtex import papers_to_bibtex
from simple_ar.literature.cache import get_cached, put_cache
from simple_ar.literature.models import Paper, normalize_paper_id
from simple_ar.literature.openalex_client import OpenAlexSearchClient, _paper_from_work
from simple_ar.literature.semantic_scholar_client import _paper_from_row as _s2_paper_from_row
from simple_ar.literature.verify import CitationError, validate_citations


class LiteratureTests(unittest.TestCase):
    def test_normalize_paper_id_keeps_citation_safe_text(self) -> None:
        self.assertEqual(normalize_paper_id("https://arxiv.org/abs/cs/9901001v1"), "cs_9901001v1")
        self.assertEqual(normalize_paper_id("2401.12345v2"), "2401.12345v2")
        from simple_ar.research.preparation_assets import validate_public_url
        for source_id in ("2401.12345v2", "cs/9901001v1"):
            with self.subTest(source_id=source_id):
                result = SimpleNamespace(get_short_id=lambda: source_id,
                    entry_id=f"http://arxiv.org/abs/{source_id}", title="A  Paper",
                    authors=["Ada Lovelace"], summary="Exact source abstract",
                    published=None, categories=["cs.LG"], doi=None)
                paper = _paper_from_result(result)
                self.assertEqual(paper.url, f"https://arxiv.org/abs/{source_id}")
                self.assertEqual(validate_public_url(paper.url), paper.url)
                self.assertEqual(paper.id, normalize_paper_id(source_id))
                self.assertEqual(paper.source_id, source_id)
                self.assertEqual(paper.abstract, result.summary)

    def test_bibtex_uses_paper_id_as_key(self) -> None:
        paper = Paper(
            id="2401.12345v1",
            title="A Test Paper",
            authors=["Ada Lovelace", "Grace Hopper"],
            abstract="Testing.",
            url="https://arxiv.org/abs/2401.12345v1",
            published="2024-01-01",
            source_id="2401.12345v1",
        )

        bibtex = papers_to_bibtex([paper])

        self.assertIn("@misc{2401.12345v1,", bibtex)
        self.assertIn("archivePrefix = {arXiv}", bibtex)

    def test_validate_citations_rejects_unknown_ids(self) -> None:
        with self.assertRaises(CitationError):
            validate_citations("Known [@paper-1], unknown [@paper-2].", {"paper-1"})

    def test_arxiv_rate_limit_message_detection(self) -> None:
        self.assertTrue(is_rate_limit_message("Page request resulted in HTTP 429"))
        self.assertTrue(is_rate_limit_message("Too many requests"))
        self.assertFalse(is_rate_limit_message("Connection reset"))

    def test_arxiv_session_adds_bounded_timeout(self) -> None:
        with patch("requests.Session.request", return_value=None) as request:
            _TimeoutSession(7).request("GET", "https://example.com")

        self.assertEqual(request.call_args.kwargs["timeout"], 7)

    def test_literature_cache_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            rows = [{"id": "paper-1", "title": "Cached Paper"}]

            put_cache("agent simulation", "arxiv", 5, rows, cache_dir=cache_dir)
            cached = get_cached("agent simulation", "arxiv", 5, cache_dir=cache_dir)

            self.assertEqual(cached, rows)

    def test_openalex_work_parses_to_project_paper_schema(self) -> None:
        paper = _paper_from_work(
            {
                "id": "https://openalex.org/W123",
                "title": "A Small Retrieval Paper",
                "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
                "publication_date": "2024-01-01",
                "doi": "https://doi.org/10.1234/example",
                "abstract_inverted_index": {"Retrieval": [0], "works": [1]},
                "ids": {"openalex": "https://openalex.org/W123"},
            }
        )

        self.assertEqual(paper.id, "openalex-W123")
        self.assertEqual(paper.source, "openalex")
        self.assertEqual(paper.authors, ["Ada Lovelace"])
        self.assertEqual(paper.abstract, "Retrieval works")
        self.assertEqual(paper.doi, "10.1234/example")

    def test_openalex_search_uses_explicit_timeout_and_query(self) -> None:
        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {
                    "results": [
                        {
                            "id": "https://openalex.org/W123",
                            "title": "A Small Retrieval Paper",
                            "authorships": [],
                            "publication_year": 2024,
                            "ids": {"openalex": "https://openalex.org/W123"},
                        }
                    ]
                }

        with patch(
            "simple_ar.literature.openalex_client.requests.get",
            return_value=FakeResponse(),
        ) as get:
            papers = OpenAlexSearchClient().search("agent coding", max_results=3)

        self.assertEqual(papers[0].id, "openalex-W123")
        self.assertEqual(get.call_args.kwargs["timeout"], 20)
        self.assertEqual(get.call_args.kwargs["params"]["search"], "agent coding")
        self.assertEqual(get.call_args.kwargs["params"]["per-page"], 3)

    def test_semantic_scholar_row_parses_to_project_paper_schema(self) -> None:
        paper = _s2_paper_from_row(
            {
                "paperId": "abc123",
                "title": "A Semantic Scholar Paper",
                "abstract": "Search metadata.",
                "year": 2025,
                "venue": "ACL",
                "authors": [{"name": "Grace Hopper"}],
                "externalIds": {"DOI": "10.1234/s2", "ArXiv": "2501.00001"},
                "url": "https://www.semanticscholar.org/paper/abc123",
            }
        )

        self.assertEqual(paper.id, "s2-abc123")
        self.assertEqual(paper.source, "semantic_scholar")
        self.assertEqual(paper.authors, ["Grace Hopper"])
        self.assertEqual(paper.published, "2025")
        self.assertEqual(paper.categories, ["ACL"])
        self.assertEqual(paper.doi, "10.1234/s2")
        self.assertEqual(paper.fulltext_url, "https://arxiv.org/pdf/2501.00001.pdf")

    def test_semantic_scholar_prefers_explicit_open_pdf_and_ignores_untrusted_scheme(self) -> None:
        with_pdf = _s2_paper_from_row({
            "paperId": "open", "title": "Open study",
            "externalIds": {"ArXiv": "2501.00001"},
            "openAccessPdf": {"url": "https://example.org/study.pdf"},
        })
        self.assertEqual(with_pdf.fulltext_url, "https://example.org/study.pdf")
        unsafe = _s2_paper_from_row({
            "paperId": "unsafe", "title": "Unknown source",
            "openAccessPdf": {"url": "file:///etc/passwd"},
        })
        self.assertIsNone(unsafe.fulltext_url)


if __name__ == "__main__":
    unittest.main()
