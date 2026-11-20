from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from simple_ar.core import CapabilityRegistry, SessionController
from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext
from simple_ar.core.budget import BudgetExceededError, BudgetLedger
from simple_ar.literature.cache import put_cache
from simple_ar.literature.models import Paper
from simple_ar.research.contracts import QueryPlan, ResearchQuestion
from simple_ar.research.sources import (
    SearchProviderRegistry,
    SearchRequest,
    SearchSelectionPolicy,
    SearchResult,
    run_search_capability,
    select_search_result,
    search_sources,
)
from simple_ar.research.sources.base import SearchQuery, SearchResponse


class SearchCapabilityTests(unittest.TestCase):
    def test_native_search_schema_and_omitted_arguments_use_the_effective_source_policy(self) -> None:
        from simple_ar.tools.sources import SourceToolGateway

        observed = []

        class Connector:
            source_name = "fixture"

            def search(self, request):
                observed.append(request)
                return SearchResponse("fixture", request.query, [])

        with tempfile.TemporaryDirectory() as directory:
            ledger = BudgetLedger({'source_operations': 2})
            owner = SourceToolGateway(Path(directory), None, ledger,
                default_policy={'providers': ['fixture', 'unregistered'], 'max_results': 3},
                registry=SearchProviderRegistry({'fixture': Connector}))
            spec = next(row for row in owner.tool_specs() if row.name == 'search_sources')
            fields = spec.parameters_schema()['properties']
            self.assertEqual(fields['providers']['items']['enum'], ['fixture'])
            self.assertEqual(fields['max_results']['maximum'], 3)
            self.assertTrue(owner.call('search_sources', {'query': 'interval calibration'}, identity='ready').ok)
            self.assertEqual(observed[0].max_results, 3)
            blocked = owner.call('search_sources',
                {'query': 'interval calibration', 'max_results': 4}, identity='invalid')
            self.assertIn('at most 3', blocked.error)
            self.assertEqual(len(observed), 1)

    def test_temporal_eligibility_precedes_topk_even_when_provider_ignores_filters(self) -> None:
        scope = {"start_year": 2010, "end_year": 2014, "basis_quote": "2010 through 2014"}
        def paper(identifier, published, strong=False):
            return Paper(id=identifier, title=f"interval method coverage {identifier}" if strong else f"interval {identifier}",
                         authors=[], abstract="interval method coverage" if strong else "",
                         url="https://example.test/" + identifier, published=published, source="fixture")
        papers = [paper("old", "2009", True), paper("future", "2015", True),
                  paper("unknown", None, True), paper("invalid", "2012-99-01", True),
                  paper("first", "2010"), paper("last", "2014-12-31")]
        class Connector:
            source_name = "fixture"
            def search(self, request):
                return SearchResponse("fixture", request.query, list(papers))  # Ignores filters.
        query = "interval method coverage"
        policy = SearchSelectionPolicy(topic="Survey 2010 through 2014", questions=(
            ResearchQuestion("RQ1", "Which methods?", facet="method"),),
            query_plan=QueryPlan(topic="interval", seed_queries=[query], queries=[query],
                query_specs=[{"query": query, "facet": "method"}], required_facets=["method"],
                max_rounds=1, auto_expansion=False), max_documents=2)
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            ledger = BudgetLedger({'source_operations': 2})
            result = run_search_capability(
                context=CapabilityContext(store, AttemptManifest("search")),
                request=SearchRequest(queries=(query,), providers=("fixture",), cache_enabled=False,
                                      filters={"temporal_scope": scope}),
                registry=SearchProviderRegistry({"fixture": Connector}), selection_policy=policy,
                budget_ledger=ledger)
            saved = store.read_json(next(ref for ref in result.artifacts if ref.kind == "search_result"))
            self.assertEqual(set(saved["selected_paper_ids"]), {"first", "last"})
            restored = SearchResult.from_handoff_dict(saved)
            self.assertEqual({p.id for p in restored.selected_papers}, {"first", "last"})
            self.assertEqual(len(restored.papers), 6)
            reasons = {row["paper_id"]: row["reason"] for row in restored.selection_rows}
            self.assertEqual(reasons["old"], "outside_temporal_scope")
            self.assertEqual(reasons["future"], "outside_temporal_scope")
            self.assertEqual(reasons["unknown"], "publication_year_unknown")
            self.assertEqual(reasons["invalid"], "publication_year_unknown")
            self.assertEqual(set(restored.coverage_report["questions"][0]["supporting_papers"]),
                             {"first", "last"})
            # Reading-driven followups have no metadata selection policy, but
            # must not reintroduce dates excluded by the original user scope.
            followup_store = ArtifactStore(Path(directory) / "followup")
            followup = run_search_capability(
                context=CapabilityContext(followup_store, AttemptManifest("followup")),
                request=SearchRequest(queries=(query,), providers=("fixture",), cache_enabled=False,
                                      filters={"temporal_scope": scope}),
                registry=SearchProviderRegistry({"fixture": Connector}), budget_ledger=ledger)
            handoff = followup_store.read_json(next(ref for ref in followup.artifacts if ref.kind == "search_result"))
            self.assertEqual(set(handoff["selected_paper_ids"]), {"first", "last"})
            self.assertEqual(len(handoff["papers"]), 6)
            self.assertEqual(ledger.remaining('source_operations'), 0)
            with self.assertRaises(BudgetExceededError):
                run_search_capability(context=CapabilityContext(store, AttemptManifest('extra')),
                    request=SearchRequest(queries=(query,), providers=('fixture',)),
                    registry=SearchProviderRegistry({"fixture": Connector}), budget_ledger=ledger)
        raw = search_sources(SearchRequest(queries=(query,), providers=("fixture",), cache_enabled=False),
                             registry=SearchProviderRegistry({"fixture": Connector}))
        legacy = select_search_result(raw, policy=policy)
        self.assertTrue(any(p.id in {"old", "future", "unknown", "invalid"} for p in legacy.selected_papers))
        # All excluded, including high lexical scores: no automatic quota filling or coverage.
        papers[:] = papers[:4]
        raw = search_sources(SearchRequest(queries=(query,), providers=("fixture",), cache_enabled=False),
                             registry=SearchProviderRegistry({"fixture": Connector}))
        empty = select_search_result(raw, policy=policy, filters={"temporal_scope": scope})
        self.assertEqual(empty.selected_papers, ())
        self.assertEqual(len(empty.papers), 4)
        self.assertEqual(empty.coverage_report["covered_facets"], [])
        self.assertEqual(empty.coverage_report["questions"][0]["status"], "missing")
        # Native-filter responses cannot pollute or recover an unrestricted cache key.
        cached_queries = []
        with tempfile.TemporaryDirectory() as directory:
            scoped_request = SearchRequest(queries=(query,), providers=("fixture",),
                filters={"temporal_scope": scope}, cache_dir=Path(directory),
                cache_put=lambda cache_query, *args, **kwargs: cached_queries.append(cache_query))
            search_sources(scoped_request, registry=SearchProviderRegistry({"fixture": Connector}))
            self.assertEqual(cached_queries, [query + " [publication_year:2010-2014]"])
        with self.assertRaises(ValueError):
            SearchRequest(queries=(query,), providers=("fixture",),
                          filters={"temporal_scope": {**scope, "start_year": True}})

    def test_successful_multi_provider_search_preserves_call_order(self) -> None:
        calls: list[tuple[str, str, int]] = []

        class Connector:
            def __init__(self, name: str) -> None:
                self.source_name = name

            def search(self, request: SearchQuery) -> SearchResponse:
                calls.append((self.source_name, request.query, request.max_results))
                return SearchResponse(
                    source=self.source_name,
                    query=request.query,
                    papers=[
                        Paper(
                            id=f"{self.source_name}-paper",
                            title="Fixture paper",
                            authors=[],
                            abstract="fixture",
                            url="https://example.test/paper",
                            source=self.source_name,
                        )
                    ],
                )

        result = search_sources(
            SearchRequest(
                queries=("first", "second"),
                providers=("one", "two"),
                max_results_per_query=3,
            ),
            registry=SearchProviderRegistry(
                {
                    "one": lambda: Connector("one"),
                    "two": lambda: Connector("two"),
                }
            ),
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(result.responses), 4)
        self.assertEqual(len(result.papers), 4)
        self.assertEqual(
            calls,
            [("one", "first", 3), ("one", "second", 3), ("two", "first", 3), ("two", "second", 3)],
        )

    def test_stop_after_papers_bounds_provider_queries(self) -> None:
        calls: list[str] = []

        class Connector:
            source_name = "fixture"

            def search(self, request: SearchQuery) -> SearchResponse:
                calls.append(request.query)
                return SearchResponse(
                    source=self.source_name,
                    query=request.query,
                    papers=[
                        Paper(
                            id=f"paper-{request.query}",
                            title=f"Paper for {request.query}",
                            authors=[],
                            abstract="bounded search fixture",
                            url=f"https://example.test/{request.query}",
                            source=self.source_name,
                        )
                    ],
                )

        result = search_sources(
            SearchRequest(
                queries=("first", "second", "third"),
                providers=("fixture",),
                stop_after_papers=2,
            ),
            registry=SearchProviderRegistry({"fixture": Connector}),
        )

        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(len(result.papers), 2)
        self.assertEqual(result.status, "completed")

    def test_partial_failure_is_not_reported_as_empty_success(self) -> None:
        import requests
        from urllib.error import HTTPError
        calls = []
        class FailingConnector:
            source_name = "broken"

            def search(self, request: SearchQuery) -> SearchResponse:
                calls.append(request.query)
                if limited:
                    response = requests.Response()
                    response.status_code = 429
                    try:
                        if limited == "urllib":
                            raise HTTPError("https://example.test/search", 429, "quota exhausted", {}, None)
                        raise requests.HTTPError("quota exhausted", response=response)
                    except (requests.HTTPError, HTTPError) as cause:
                        raise RuntimeError("provider wrapper") from cause
                raise RuntimeError("service unavailable")

        class WorkingConnector:
            source_name = "working"

            def search(self, request: SearchQuery) -> SearchResponse:
                return SearchResponse(
                    source=self.source_name,
                    query=request.query,
                    papers=[],
                )

        for limited in (False, "requests", "urllib"):
            with self.subTest(rate_limited=limited):
                calls.clear()
                result = search_sources(
                    SearchRequest(queries=("first", "second"), providers=("broken", "working")),
                    registry=SearchProviderRegistry(
                        {"broken": FailingConnector, "working": WorkingConnector}
                    ),
                )
                self.assertEqual(result.status, "partial")
                self.assertEqual(result.responses[0].status, "rate_limited" if limited else "failed")
                self.assertEqual(len(result.diagnostics), 2)
                self.assertEqual(len(result.responses), 4)
                self.assertEqual(calls, ["first"] if limited else ["first", "second"])
                restored = SearchResult.from_handoff_dict(result.to_handoff_dict())
                self.assertEqual(restored.responses[1].query, "second")
                self.assertEqual(restored.responses[1].status, result.responses[1].status)
                if limited:
                    self.assertIn("Skipped live query", restored.responses[1].message)
                    only_limited = search_sources(
                        SearchRequest(queries=("first", "second"), providers=("broken",)),
                        registry=SearchProviderRegistry({"broken": FailingConnector}))
                    self.assertEqual(only_limited.status, "failed")

    def test_all_provider_failures_are_failed(self) -> None:
        registry = SearchProviderRegistry({"missing": lambda: (_ for _ in ()).throw(KeyError("client"))})

        result = search_sources(
            SearchRequest(queries=("query",), providers=("missing",)),
            registry=registry,
        )

        self.assertEqual(result.status, "failed")
        self.assertFalse(result.papers)
        self.assertIn("KeyError", result.diagnostics[0])

    def test_all_successful_empty_responses_are_empty(self) -> None:
        class EmptyConnector:
            source_name = "empty"

            def search(self, request: SearchQuery) -> SearchResponse:
                return SearchResponse(source=self.source_name, query=request.query, papers=[])

        result = search_sources(
            SearchRequest(queries=("query",), providers=("empty",)),
            registry=SearchProviderRegistry({"empty": EmptyConnector}),
        )

        self.assertEqual(result.status, "empty")
        self.assertEqual(result.to_dict()["schema_version"], "search_result.v1")

    def test_optional_cache_recovers_provider_failure_without_hiding_it(self) -> None:
        import requests
        calls = []
        paper = Paper(
            id="cached-paper",
            title="Cached reliable agents",
            authors=[],
            abstract="Cached metadata for a bounded search.",
            url="https://example.test/cached",
            source="fixture",
        )

        class FailingConnector:
            source_name = "fixture"

            def search(self, request: SearchQuery) -> SearchResponse:
                calls.append(request.query)
                response = requests.Response()
                response.status_code = 429
                raise requests.HTTPError("provider unavailable", response=response)

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "literature"
            put_cache(
                "query",
                "fixture",
                10,
                [paper.to_row()],
                cache_dir=cache_dir,
            )
            put_cache("second", "fixture", 10, [paper.to_row()], cache_dir=cache_dir)
            result = search_sources(
                SearchRequest(
                    queries=("query", "second"),
                    providers=("fixture",),
                    cache_dir=cache_dir,
                ),
                registry=SearchProviderRegistry({"fixture": FailingConnector}),
            )

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.responses[0].status, "cached")
        self.assertEqual(result.responses[0].papers[0].id, "cached-paper")
        self.assertIn("provider unavailable", result.responses[0].message)
        self.assertEqual(calls, ["query"])
        self.assertEqual(result.responses[1].status, "cached")
        self.assertIn("Skipped live query", result.responses[1].message)

    def test_optional_cache_persists_successful_metadata(self) -> None:
        paper = Paper(
            id="fresh-paper",
            title="Fresh reliable agents",
            authors=[],
            abstract="Fresh metadata.",
            url="https://example.test/fresh",
            source="fixture",
        )

        class Connector:
            source_name = "fixture"

            def search(self, request: SearchQuery) -> SearchResponse:
                return SearchResponse(
                    source=self.source_name,
                    query=request.query,
                    papers=[paper],
                )

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "literature"
            result = search_sources(
                SearchRequest(
                    queries=("query",),
                    providers=("fixture",),
                    cache_dir=cache_dir,
                ),
                registry=SearchProviderRegistry({"fixture": Connector}),
            )
            cached = search_sources(
                SearchRequest(
                    queries=("query",),
                    providers=("fixture",),
                    cache_dir=cache_dir,
                ),
                registry=SearchProviderRegistry(
                    {"fixture": lambda: (_ for _ in ()).throw(RuntimeError("offline"))}
                ),
            )

        self.assertEqual(result.status, "completed")
        self.assertEqual(cached.status, "partial")
        self.assertEqual(cached.responses[0].status, "cached")
        self.assertEqual(cached.papers[0].id, "fresh-paper")

    def test_search_capability_persists_full_handoff_without_duplicate_rows(self) -> None:
        paper = Paper(
            id="paper-1",
            title="Fixture paper",
            authors=[],
            abstract="A fixture result.",
            url="https://example.test/paper",
            source="fixture",
        )
        available = [paper]

        class Connector:
            source_name = "fixture"

            def search(self, request: SearchQuery) -> SearchResponse:
                return SearchResponse(
                    source=self.source_name,
                    query=request.query,
                    papers=list(available),
                )

        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            registry.register("search", run_search_capability)
            controller = SessionController.create(
                Path(tmp),
                session_id="search-handoff",
                topic="fixture search",
                registry=registry,
            )
            result = controller.execute_attempt(
                "search",
                attempt_id="attempt-001",
                request=SearchRequest(queries=("fixture",), providers=("fixture",)),
                registry=SearchProviderRegistry({"fixture": Connector}),
            )

            self.assertEqual(result.status, "completed")
            handoff = controller.store.read_json(
                "attempts/attempt-001/search_result.json"
            )
            self.assertEqual(handoff["schema_version"], "search_handoff.v1")
            self.assertEqual([row["id"] for row in handoff["papers"]], ["paper-1"])
            self.assertEqual(handoff["responses"][0]["paper_ids"], ["paper-1"])
            self.assertEqual(handoff["responses"][0]["message"], "")
            restored = SearchResult.from_handoff_dict(handoff)
            self.assertEqual(restored.status, "completed")
            self.assertEqual(restored.papers[0].id, "paper-1")
            self.assertEqual(restored.responses[0].papers[0].title, "Fixture paper")
            # Semantic selection sees eligible candidates before acquisition
            # truncation, including a candidate with no lexical query overlap.
            from unittest.mock import patch
            available.append(Paper(id="implementation", title="SDK reference", authors=[],
                abstract="", url="https://example.test/sdk", source="fixture"))
            policy = SearchSelectionPolicy(topic="fixture", questions=(),
                query_plan=QueryPlan(topic="fixture", seed_queries=["fixture"], queries=["fixture"], max_rounds=1,
                                     auto_expansion=False), max_documents=1)
            def select(client, **kwargs):
                self.assertEqual({r["id"] for r in kwargs["papers"]}, {"paper-1", "implementation"})
                self.assertEqual(kwargs["config"]["read_screening_max_shortlist"], 1)
                return [{"paper_id": "implementation", "decision": "keep"}]
            with patch("simple_ar.research.evidence.screening.screen_papers_with_llm", side_effect=select):
                controller.execute_attempt("search", attempt_id="attempt-002",
                    request=SearchRequest(queries=("fixture",), providers=("fixture",), llm_client=object()),
                    registry=SearchProviderRegistry({"fixture": Connector}), selection_policy=policy)
            selected = controller.store.read_json("attempts/attempt-002/search_result.json")
            self.assertEqual(selected["selected_paper_ids"], ["implementation"])
            self.assertEqual(len(selected["papers"]), 2)
            self.assertEqual(selected["coverage"]["semantic_selection"]["scope"],
                             "candidate_metadata_not_read_evidence")
            with patch("simple_ar.research.evidence.screening.screen_papers_with_llm",
                       side_effect=RuntimeError("model unavailable")):
                failed = controller.execute_attempt("search", attempt_id="attempt-003",
                    request=SearchRequest(queries=("fixture",), providers=("fixture",), llm_client=object()),
                    registry=SearchProviderRegistry({"fixture": Connector}), selection_policy=policy)
            self.assertEqual(failed.status, "failed")
            discovery = controller.store.read_json("attempts/attempt-003/search_result.json")
            self.assertEqual(len(discovery["papers"]), 2)
            self.assertNotIn("semantic_selection", discovery["coverage"])
            # Linked originals and newly discovered sources compete for the
            # same single slot, through one existing semantic selection call.
            from simple_ar.research.contracts import DocumentRecord
            linked = DocumentRecord("linked", "Author implementation", "supporting_material",
                url="https://example.test/author-code",
                metadata={"kind": "supporting_material", "parent_quote": "The author publishes code here."})
            for chosen in ("implementation", "linked"):
                with self.subTest(chosen=chosen):
                    def select_joint(client, **kwargs):
                        self.assertEqual({r["id"] for r in kwargs["papers"]},
                                         {"paper-1", "implementation", "linked"})
                        candidate = next(r for r in kwargs["papers"] if r["id"] == "linked")
                        self.assertEqual(candidate["abstract"], linked.metadata["parent_quote"])
                        self.assertEqual(candidate["source"], "supporting_material")
                        self.assertEqual(kwargs["config"]["read_screening_max_shortlist"], 1)
                        return [{"paper_id": chosen, "decision": "keep"}]
                    with patch("simple_ar.research.evidence.screening.screen_papers_with_llm",
                               side_effect=select_joint) as screen:
                        attempt = "joint-" + chosen
                        run_search_capability(context=CapabilityContext(
                            ArtifactStore(Path(tmp) / attempt), AttemptManifest(attempt)),
                            request=SearchRequest(queries=("fixture",), providers=("fixture",), llm_client=object()),
                            registry=SearchProviderRegistry({"fixture": Connector}),
                            selection_policy=policy, linked_records=(linked,))
                    screen.assert_called_once()
                    saved = ArtifactStore(Path(tmp) / attempt).read_json("search_result.json")
                    self.assertEqual(saved["selected_paper_ids"], [chosen])
                    self.assertEqual(len(SearchResult.from_handoff_dict(saved).papers), 3)
                    self.assertIn("no provider request", saved["responses"][-1]["message"])
            self.assertEqual(linked.abstract, "")  # Candidate projection cannot mutate the original.

    def test_search_handoff_reports_broken_paper_reference(self) -> None:
        restored = SearchResult.from_handoff_dict(
            {
                "schema_version": "search_handoff.v1",
                "status": "partial",
                "papers": [],
                "responses": [
                    {
                        "source": "fixture",
                        "query": "query",
                        "status": "ok",
                        "paper_ids": ["missing-paper"],
                    }
                ],
            }
        )

        self.assertEqual(restored.status, "partial")
        self.assertFalse(restored.responses[0].papers)
        self.assertIn("missing-paper", restored.diagnostics[0])

    def test_selection_policy_persists_deduplication_and_coverage(self) -> None:
        first = Paper(
            id="paper-1",
            title="Reliable agent method",
            authors=[],
            abstract="A method improves benchmark accuracy.",
            url="https://example.test/one",
            source="fixture",
        )
        duplicate = Paper(
            id="paper-duplicate",
            title="Reliable agent method",
            authors=[],
            abstract="A method improves accuracy.",
            url="https://example.test/two",
            source="fixture",
        )
        second = Paper(
            id="paper-2",
            title="Reliable agent benchmark",
            authors=[],
            abstract="A benchmark measures accuracy.",
            url="https://example.test/three",
            source="fixture",
        )

        class Connector:
            source_name = "fixture"

            def search(self, request: SearchQuery) -> SearchResponse:
                papers = [first, duplicate] if request.query == "method" else [second]
                return SearchResponse(
                    source=self.source_name,
                    query=request.query,
                    papers=papers,
                )

        raw = search_sources(
            SearchRequest(
                queries=("method", "benchmark"),
                providers=("fixture",),
            ),
            registry=SearchProviderRegistry({"fixture": Connector}),
        )
        selected = select_search_result(
            raw,
            policy=SearchSelectionPolicy(
                topic="reliable agents",
                questions=(
                    ResearchQuestion(
                        question_id="RQ1",
                        question="What method is used?",
                        facet="method",
                    ),
                    ResearchQuestion(
                        question_id="RQ2",
                        question="How is it evaluated?",
                        facet="benchmark",
                    ),
                ),
                query_plan=QueryPlan(
                    topic="reliable agents",
                    seed_queries=["method", "benchmark"],
                    queries=["method", "benchmark"],
                    query_specs=[
                        {"query": "method", "facet": "method"},
                        {"query": "benchmark", "facet": "benchmark"},
                    ],
                    required_facets=["method", "benchmark"],
                ),
                max_documents=2,
            ),
        )

        self.assertEqual(len(raw.papers), 3)
        self.assertEqual([paper.id for paper in selected.selected_papers], ["paper-1", "paper-2"])
        self.assertEqual(selected.coverage_report["status"], "covered")
        self.assertTrue(any(row["reason"] == "duplicate_lower_score" for row in selected.selection_rows))
        restored = SearchResult.from_handoff_dict(selected.to_handoff_dict())
        self.assertEqual([paper.id for paper in restored.selected_papers], ["paper-1", "paper-2"])
        self.assertEqual(restored.coverage_report["covered_facets"], ["benchmark", "method"])

    def test_explicit_empty_selection_is_not_widened_to_raw_papers(self) -> None:
        raw = Paper(
            id="paper-raw",
            title="Out-of-scope paper",
            authors=[],
            abstract="A paper intentionally rejected by the selection policy.",
            url="https://example.test/raw",
            source="fixture",
            source_id="raw",
        )
        result = SearchResult(
            status="completed",
            responses=(
                SearchResponse(
                    source="fixture",
                    query="topic",
                    papers=[raw],
                ),
            ),
            papers=(raw,),
            selected_papers=(),
            selection_rows=(
                {"paper_id": raw.id, "decision": "drop", "reason": "out of scope"},
            ),
            coverage_report={"status": "partial"},
        )

        restored = SearchResult.from_handoff_dict(result.to_handoff_dict())

        self.assertEqual([paper.id for paper in restored.papers], [raw.id])
        self.assertEqual(restored.selected_papers, ())

    def test_search_rounds_accumulate_candidates_and_stop_within_limits(self) -> None:
        for mode, max_rounds, query_limit, expansion, expected_rounds, reason in (
            ("new", 4, 1, True, 2, "follow_up_query_limit_reached"),
            ("new", 2, 3, True, 2, "round_limit_reached"),
            ("duplicate", 4, 3, True, 2, "no_new_candidates"),
            ("failed", 4, 3, True, 2, "providers_failed"),
            ("new", 4, 0, True, 1, "follow_up_query_limit_reached"),
            ("new", 4, 3, False, 1, "query_expansion_disabled"),
        ):
            with self.subTest(mode=mode, reason=reason), tempfile.TemporaryDirectory() as tmp:
                calls = []

                class Connector:
                    source_name = "fixture"

                    def search(self, request: SearchQuery) -> SearchResponse:
                        calls.append(request.query)
                        if mode == "failed" and len(calls) > 1:
                            raise RuntimeError("follow-up unavailable")
                        duplicate = mode == "duplicate" or len(calls) == 1
                        paper = Paper(
                            id=f"paper-{len(calls)}", authors=[], source="fixture",
                            title="Reliable method" if duplicate else "Reliable benchmark",
                            abstract="A reliable method." if duplicate else "A benchmark evaluation.",
                            doi="10.fixture/method" if duplicate else "10.fixture/benchmark",
                            url="https://example.test/paper",
                        )
                        return SearchResponse(source=self.source_name, query=request.query, papers=[paper])

                capabilities = CapabilityRegistry()
                capabilities.register("search", run_search_capability)
                controller = SessionController.create(
                    Path(tmp), session_id="search-rounds", topic="reliable", registry=capabilities,
                )
                result = controller.execute_attempt(
                    "search", attempt_id="attempt-001",
                    request=SearchRequest(queries=("method", " METHOD "), providers=("fixture", "fixture")),
                    registry=SearchProviderRegistry({"fixture": Connector}),
                    selection_policy=SearchSelectionPolicy(
                        topic="reliable", questions=(), max_documents=3, next_query_limit=query_limit,
                        query_plan=QueryPlan(
                            topic="reliable", seed_queries=["method"], queries=["method"],
                            query_specs=[{"query": "method", "facet": "method"}],
                            required_facets=["method", "benchmark", "dataset"],
                            max_rounds=max_rounds, auto_expansion=expansion,
                        ),
                    ),
                )
                handoff = controller.store.read_json("attempts/attempt-001/search_result.json")
                restored = SearchResult.from_handoff_dict(handoff)
                coverage = restored.coverage_report
                expected_queries = 1 + (min(query_limit, 2) if expected_rounds == 2 else 0)
                response_rounds = (1,) + (2,) * (expected_queries - 1)
                self.assertEqual(len(calls), expected_queries)
                self.assertEqual(len({query.casefold() for query in calls}), len(calls))
                self.assertEqual(restored.response_rounds, response_rounds)
                self.assertEqual(result.usage["query_count"], expected_queries)
                self.assertEqual(result.usage["round_count"], expected_rounds)
                self.assertEqual(coverage["retrieval"]["executed_rounds"], expected_rounds)
                self.assertEqual([row["round"] for row in coverage["retrieval"]["attempts"]], list(restored.response_rounds))
                self.assertEqual(coverage["stop_reason"], reason)
                self.assertEqual(coverage["scope"], "search_metadata_only")
                self.assertEqual(coverage["semantic_verification"], "not_performed")
                self.assertIn("paper-1", [paper.id for paper in restored.selected_papers])
                if mode == "new" and expected_rounds == 2:
                    self.assertEqual(len(restored.selected_papers), 2)
                    self.assertEqual({row["round"] for row in restored.selection_rows if row["decision"] == "keep"}, {1, 2})
                elif mode == "duplicate":
                    self.assertEqual(len(restored.papers), expected_queries)
                    self.assertEqual(len(restored.selected_papers), 1)
                elif mode == "failed":
                    self.assertEqual(result.status, "partial")
                    self.assertEqual(restored.responses[-1].status, "failed")
                    self.assertIn("follow-up unavailable", restored.diagnostics[-1])

    def test_search_rounds_do_not_repeat_executed_fallback_query(self) -> None:
        class Connector:
            source_name = "fixture"

            def search(self, request: SearchQuery) -> SearchResponse:
                return SearchResponse(source=self.source_name, query=request.query, papers=[Paper(
                    id="paper-1", title="Reliable method", authors=[], abstract="A method.",
                    url="https://example.test/paper", source=self.source_name,
                )])

        with tempfile.TemporaryDirectory() as tmp:
            capabilities = CapabilityRegistry()
            capabilities.register("search", run_search_capability)
            controller = SessionController.create(Path(tmp), session_id="no-repeat", topic="reliable", registry=capabilities)
            result = controller.execute_attempt(
                "search", attempt_id="attempt-001",
                request=SearchRequest(queries=("reliable benchmark evaluation metric",), providers=("fixture",)),
                registry=SearchProviderRegistry({"fixture": Connector}),
                selection_policy=SearchSelectionPolicy(
                    topic="reliable", questions=(), max_documents=1, next_query_limit=3,
                    query_plan=QueryPlan(topic="reliable", seed_queries=[], required_facets=["benchmark"], max_rounds=4),
                ),
            )
            handoff = controller.store.read_json("attempts/attempt-001/search_result.json")
            self.assertEqual(result.usage["query_count"], 1)
            self.assertEqual(handoff["coverage"]["stop_reason"], "no_new_queries")

    def test_request_rejects_empty_queries_providers_and_limits(self) -> None:
        with self.assertRaises(ValueError):
            SearchRequest(queries=(), providers=("fixture",))
        with self.assertRaises(ValueError):
            SearchRequest(queries=("query",), providers=())
        with self.assertRaises(ValueError):
            SearchRequest(queries=("query",), providers=("fixture",), max_results_per_query=0)


if __name__ == "__main__":
    unittest.main()
