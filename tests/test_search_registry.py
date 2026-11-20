from __future__ import annotations

from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from simple_ar.literature.models import Paper
from simple_ar.research.sources import SearchProviderRegistry, default_search_provider_registry, SearchRequest, search_sources
from simple_ar.research.sources.base import SearchQuery, SearchResponse


TEST_ROOT = Path(__file__).resolve().parents[1] / ".tmp_tests"


class SearchProviderRegistryTests(unittest.TestCase):
    def test_known_title_is_resolved_from_provider_results(self) -> None:
        from simple_ar.research.connectors.arxiv import ArxivConnector

        client = Mock()
        title = 'Measuring uncertainty: A comparison'
        client.search.return_value = [
            Paper(id='other', title='A comparison of linear models', authors=[], abstract='', url=''),
            Paper(id='matched', title='Measuring uncertainty — a comparison', authors=[], abstract='', url=''),
        ]
        registry = SearchProviderRegistry({'arxiv': lambda: ArxivConnector(client)})
        result = search_sources(SearchRequest((title,), ('arxiv',), filters={'match_title': True}),
            registry=registry)
        client.search.assert_called_once_with(f'ti:"{title}"', max_results=10)
        self.assertEqual([paper.id for paper in result.papers], ['matched'])
        self.assertEqual([paper.id for paper in result.responses[0].papers], ['matched'])

    def test_native_temporal_filters_use_existing_connectors_and_http_params(self) -> None:
        from simple_ar.research.connectors.arxiv import ArxivConnector
        from simple_ar.research.connectors.openalex import OpenAlexConnector
        from simple_ar.research.connectors.semantic_scholar import SemanticScholarConnector
        from simple_ar.literature.openalex_client import OpenAlexSearchClient, OpenAlexSearchError
        from simple_ar.literature.semantic_scholar_client import SemanticScholarSearchClient, SemanticScholarSearchError
        scope = {"start_year": 2010, "end_year": 2014, "basis_quote": "2010 through 2014"}
        request = SearchQuery("interval method", max_results=8, filters={"temporal_scope": scope})
        arxiv = Mock()
        arxiv.search.return_value = []
        response = ArxivConnector(arxiv).search(request)
        arxiv.search.assert_called_once_with(
            "(all:interval AND all:method) AND submittedDate:[201001010000 TO 201412312359]", max_results=8)
        self.assertEqual(response.query, "interval method")  # Trace/round identity is unchanged.
        with patch("simple_ar.literature.openalex_client._respect_rate_limit"), \
             patch.dict(os.environ, {"OPENALEX_API_KEY": "fake-openalex-key"}), \
             patch("simple_ar.literature.openalex_client.requests.get") as get:
            get.return_value.json.return_value = {"results": []}
            OpenAlexConnector(OpenAlexSearchClient()).search(request)
            self.assertEqual(get.call_args.kwargs["params"]["filter"], "publication_year:2010-2014")
            self.assertEqual(get.call_args.kwargs["params"]["per-page"], 8)
            self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer fake-openalex-key")
            self.assertNotIn("api_key", get.call_args.kwargs["params"])
            self.assertEqual(OpenAlexSearchClient(api_key="").api_key, "")
            with self.assertRaises(OpenAlexSearchError):
                OpenAlexSearchClient().search("x", year_range=(True, 2014))
            self.assertEqual(get.call_count, 1)
        with patch.dict(os.environ, {"SEMANTIC_SCHOLAR_API_KEY": "fake-s2-key"}):
            client = SemanticScholarSearchClient()
            self.assertEqual(SemanticScholarSearchClient(api_key="").api_key, "")
        with patch("simple_ar.literature.semantic_scholar_client._respect_rate_limit"), \
             patch.object(client, "_request_json", return_value={"data": []}) as fetch:
            SemanticScholarConnector(client).search(request)
            params = parse_qs(urlsplit(fetch.call_args.args[0]).query)
            self.assertEqual(params["year"], ["2010-2014"])
            self.assertEqual(params["query"], ["interval method"])
            self.assertEqual(params["limit"], ["8"])
            self.assertEqual(fetch.call_args.args[1]["x-api-key"], "fake-s2-key")
            self.assertNotIn("api_key", params)
            with self.assertRaises(SemanticScholarSearchError):
                client.search("x", year_range=(2014, 2010))
            self.assertEqual(fetch.call_count, 1)
        # Old plans preserve signatures and don't send an optional filter.
        arxiv.reset_mock()
        ArxivConnector(arxiv).search(SearchQuery("interval method", 8))
        arxiv.search.assert_called_once_with("all:interval AND all:method", max_results=8)
        for query in ('ti:"interval method"', '(all:interval OR all:coverage) AND cat:stat.ML'):
            arxiv.reset_mock()
            ArxivConnector(arxiv).search(SearchQuery(query, 8))
            arxiv.search.assert_called_once_with(query, max_results=8)

    def test_registry_registers_and_resolves_lazy_connectors(self) -> None:
        calls: list[str] = []

        class Connector:
            source_name = "fixture"

            def search(self, request: SearchQuery) -> SearchResponse:
                return SearchResponse(
                    source=self.source_name,
                    query=request.query,
                    papers=[],
                )

        registry = SearchProviderRegistry()
        registry.register(" fixture ", lambda: calls.append("created") or Connector())

        self.assertEqual(registry.names(), ("fixture",))
        self.assertTrue(registry.has("fixture"))
        self.assertEqual(calls, [])
        self.assertEqual(registry.resolve("fixture").source_name, "fixture")
        self.assertEqual(calls, ["created"])

    def test_registry_rejects_duplicate_unknown_and_invalid_providers(self) -> None:
        registry = SearchProviderRegistry({"fixture": lambda: object()})

        with self.assertRaises(ValueError):
            registry.register("fixture", lambda: object())
        with self.assertRaises(TypeError):
            registry.resolve("fixture")
        with self.assertRaisesRegex(KeyError, "Unknown search provider"):
            registry.resolve("missing")

        class MissingSourceName:
            def search(self, request: SearchQuery) -> SearchResponse:
                return SearchResponse(source="fixture", query=request.query, papers=[])

        registry.register("missing-name", MissingSourceName)
        with self.assertRaisesRegex(TypeError, "source_name"):
            registry.resolve("missing-name")

    def test_factory_errors_are_not_misreported_as_unknown_provider(self) -> None:
        def failing_factory():
            raise KeyError("client")

        registry = SearchProviderRegistry({"fixture": failing_factory})

        with self.assertRaisesRegex(KeyError, "client"):
            registry.resolve("fixture")

    def test_default_registry_exposes_only_builtin_provider_names(self) -> None:
        registry = default_search_provider_registry()

        self.assertEqual(
            registry.names(),
            ("arxiv", "local_files", "openalex", "semantic_scholar", "web"),
        )
        from simple_ar.research.connectors.web import WebConnector
        import requests
        with patch.dict(os.environ, {"TAVILY_API_KEY": ""}), \
             patch("simple_ar.research.connectors.web.requests.post") as post:
            with self.assertRaisesRegex(ValueError, "TAVILY_API_KEY"):
                registry.resolve("web").search(SearchQuery("project installation"))
            post.assert_not_called()
        with patch("simple_ar.research.connectors.web.requests.post") as post:
            post.return_value.json.return_value = {"results": [
                {"url": "https://example.org/docs", "title": "Project documentation", "content": "Install notes",
                 "published_date": "Tue, 11 Mar 2025 17:00:00 GMT"},
                {"url": "https://example.org/docs", "title": "duplicate"},
                {"url": "file:///private"}, {"url": "https://example.org/repo", "published_date": "unknown"}],
                "usage": {"credits": 1}}
            connector = WebConnector("fixture-key")
            response = connector.search(SearchQuery("project installation", 4))
            self.assertEqual(len(response.papers), 2)
            self.assertEqual(response.papers[0].source, "web")
            self.assertEqual(response.papers[0].published, "2025-03-11")
            self.assertIsNone(response.papers[1].published)
            self.assertEqual(response.papers[0].authors, [])
            self.assertEqual(response.papers[0].fulltext_url, response.papers[0].url)
            self.assertIn("credits=1", response.message)
            payload = post.call_args.kwargs["json"]
            self.assertEqual(payload["search_depth"], "basic")
            self.assertFalse(payload["auto_parameters"])
            self.assertFalse(payload["include_answer"])
            self.assertNotIn("fixture-key", str(payload))
            connector.search(SearchQuery("project installation", 4, filters={"temporal_scope": {
                "start_year": 2020, "end_year": 2025, "basis_quote": "2020 through 2025"}}))
            self.assertTrue(post.call_args.kwargs["json"]["filter_by_published_date"])
            post.return_value.raise_for_status.side_effect = requests.HTTPError("429")
            with self.assertRaises(requests.HTTPError):
                connector.search(SearchQuery("another topic"))

    def test_search_accepts_a_replacement_provider_registry(self) -> None:
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            class ReplacementConnector:
                source_name = "custom_source"

                def search(self, request: SearchQuery) -> SearchResponse:
                    return SearchResponse(
                        source=self.source_name,
                        query=request.query,
                        papers=[
                            Paper(
                                id="replacement-paper",
                                title="Replacement Paper",
                                authors=[],
                                abstract="A fixture result from a replacement connector.",
                                url="https://example.test/replacement",
                                source=self.source_name,
                                source_id="replacement-paper",
                            )
                        ],
                    )

            registry = SearchProviderRegistry({"custom_source": ReplacementConnector})
            result = search_sources(
                SearchRequest(queries=("fixture topic",), providers=("custom_source",),
                              max_results_per_query=1, cache_dir=root / "cache"),
                registry=registry,
            )
            self.assertEqual([paper.id for paper in result.papers], ["replacement-paper"])


if __name__ == "__main__":
    unittest.main()
