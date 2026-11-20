"""Small standalone search capability over registered literature providers.

The capability owns provider invocation and failure normalization. Callers
still supply the query/selection policy, while this boundary can apply that
bounded policy, retain raw provider responses, and optionally persist/recover
metadata through an explicit cache directory. Document ingest and stage
artifact projection remain separate capabilities.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, TypeVar
from urllib.error import HTTPError

from simple_ar.core.capabilities import CapabilityContext, CapabilityResult
from simple_ar.core.budget import BudgetLedger
from simple_ar.literature.cache import get_cached, put_cache
from simple_ar.literature.models import Paper
from simple_ar.research.contracts import DocumentRecord, QueryPlan, ResearchQuestion
from simple_ar.research.sources.base import SearchQuery, SearchResponse, temporal_scope
from simple_ar.research.sources.registry import SearchProviderRegistry


SearchStatus = Literal["completed", "partial", "empty", "failed"]
_Result = TypeVar('_Result')


def run_source_operation(ledger: BudgetLedger | None, reservation: str, count: int,
                         operation: Callable[[], _Result], *, purpose: str, attempt_id: str = '',
                         session_id: str = '', outcome_reason: Callable[[_Result], str] | None = None) -> _Result:
    """Account admitted adapter work separately from HTTP requests and provider charges."""
    if ledger is None:
        return operation()
    if any(entry.reservation_id == reservation for entry in ledger.entries):
        raise ValueError('Source operation already recorded; inspect its original result before retry')
    amount = {'source_operations': count}
    ledger.reserve(reservation, amount, attempt_id=attempt_id, session_id=session_id, purpose=purpose)
    try:
        result = operation()
    except Exception:
        ledger.settle(reservation, amount, actual_source='adapter', reason='Source adapter work failed')
        raise
    except BaseException:
        ledger.mark_unknown(reservation, reason='Source operation interrupted; inspect retained results')
        raise
    ledger.settle(reservation, amount, actual_source='adapter',
        reason=outcome_reason(result) if outcome_reason is not None else '')
    return result


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """Provider-neutral request for one or more planned search queries."""

    queries: tuple[str, ...]
    providers: tuple[str, ...]
    max_results_per_query: int = 10
    filters: dict[str, object] = field(default_factory=dict)
    cache_dir: Path | None = None
    cache_enabled: bool = True
    llm_client: Any | None = field(default=None, repr=False, compare=False)
    problem_markdown: str = ""
    research_plan_json: str = "{}"
    screening_config: Mapping[str, object] = field(default_factory=dict)
    stop_after_papers: int | None = None
    cache_get: Callable[..., list[dict[str, Any]] | None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    cache_put: Callable[..., object] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        temporal_scope(self.filters, topic=self.filters.get("topic") if isinstance(self.filters.get("topic"), str) else None)
        queries = tuple(_new_queries(self.queries, set()))
        providers = tuple(dict.fromkeys(provider.strip() for provider in self.providers if provider.strip()))
        if not queries:
            raise ValueError("SearchRequest requires at least one query.")
        if not providers:
            raise ValueError("SearchRequest requires at least one provider.")
        if self.max_results_per_query < 1:
            raise ValueError("max_results_per_query must be positive.")
        if self.stop_after_papers is not None and self.stop_after_papers < 1:
            raise ValueError("stop_after_papers must be positive when provided.")
        object.__setattr__(self, "queries", queries)
        object.__setattr__(self, "providers", providers)
        if self.cache_dir is not None:
            object.__setattr__(self, "cache_dir", Path(self.cache_dir))


@dataclass(frozen=True, slots=True)
class SearchSelectionPolicy:
    """Bounded, deterministic selection policy for one search handoff.

    Provider invocation remains independent from selection.  The canonical
    session supplies this policy after planning so raw provider responses stay
    inspectable while downstream ingest receives one deduplicated, budgeted
    paper set.  This is the small portion of the former search-stage policy
    that is useful to every research entry point.
    """

    topic: str
    questions: tuple[ResearchQuestion, ...]
    query_plan: QueryPlan
    max_documents: int
    next_query_limit: int = 0  # Total additional queries in this search attempt.

    def __post_init__(self) -> None:
        if not self.topic.strip():
            raise ValueError("SearchSelectionPolicy.topic cannot be empty.")
        if self.max_documents < 1:
            raise ValueError("SearchSelectionPolicy.max_documents must be positive.")
        if type(self.next_query_limit) is not int or self.next_query_limit < 0:
            raise ValueError("SearchSelectionPolicy.next_query_limit must be a nonnegative integer.")
        object.__setattr__(self, "questions", tuple(self.questions))


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Normalized result for a multi-provider search attempt.

    ``responses`` retains one response per provider/query pair, including
    failures. ``papers`` is the flattened raw provider output in call order;
    ``selected_papers`` is the optional bounded result of an explicit
    ``SearchSelectionPolicy``.
    """

    status: SearchStatus
    responses: tuple[SearchResponse, ...]
    papers: tuple[Paper, ...] = ()
    diagnostics: tuple[str, ...] = ()
    selected_papers: tuple[Paper, ...] = ()
    selection_rows: tuple[dict[str, Any], ...] = ()
    coverage_report: dict[str, Any] = field(default_factory=dict)
    response_rounds: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.response_rounds and (
            len(self.response_rounds) != len(self.responses)
            or any(type(value) is not int or value < 1 for value in self.response_rounds)
        ):
            raise ValueError("response_rounds must assign a positive round to each response.")

    def to_dict(self) -> dict[str, Any]:
        """Return a compact, JSON-serializable result summary."""
        return {
            "schema_version": "search_result.v1",
            "status": self.status,
            "paper_count": len(self.papers),
            "selected_paper_count": len(self.selected_papers),
            "response_count": len(self.responses),
            "responses": [
                {
                    "source": response.source,
                    "query": response.query,
                    "status": response.status,
                    "paper_count": len(response.papers),
                    "message": response.message,
                }
                for response in self.responses
            ],
            "selection_count": len(self.selection_rows),
            "coverage_status": str(self.coverage_report.get("status") or "unknown"),
            "diagnostics": list(self.diagnostics),
        }

    def to_handoff_dict(self) -> dict[str, Any]:
        """Return provider output needed by a downstream document capability.

        The status view above intentionally omits paper rows.  This handoff
        keeps one copy of the normalized metadata and only references papers
        by ID inside each provider response, so later stages can distinguish
        usable results from source failures without copying full paper rows.
        """

        return {
            "schema_version": "search_handoff.v1",
            "status": self.status,
            "papers": [paper.to_row() for paper in {paper.id: paper for paper in self.papers}.values()],
            "selected_paper_ids": [paper.id for paper in self.selected_papers],
            "responses": [
                {
                    "source": response.source,
                    "query": response.query,
                    "status": response.status,
                    "paper_ids": [paper.id for paper in response.papers],
                    "message": response.message,
                    "round": self.response_rounds[index] if self.response_rounds else 1,
                }
                for index, response in enumerate(self.responses)
            ],
            "selection": [dict(row) for row in self.selection_rows],
            "coverage": dict(self.coverage_report),
            "diagnostics": list(self.diagnostics),
        }

    @classmethod
    def from_handoff_dict(cls, data: Mapping[str, Any]) -> "SearchResult":
        """Restore a persisted ``search_handoff.v1`` without network access."""
        if str(data.get("schema_version") or "") != "search_handoff.v1":
            raise ValueError("Expected a search_handoff.v1 object.")
        status = str(data.get("status") or "")
        if status not in {"completed", "partial", "empty", "failed"}:
            raise ValueError(f"Unsupported search handoff status: {status!r}")

        papers = tuple(
            Paper.from_row(row)
            for row in data.get("papers", [])
            if isinstance(row, Mapping)
        )
        paper_by_id = {paper.id: paper for paper in papers}
        selected_ids = [
            str(item).strip()
            for item in data.get("selected_paper_ids", [])
            if str(item).strip()
        ]
        responses: list[SearchResponse] = []
        response_rounds: list[int] = []
        diagnostics = [str(item) for item in data.get("diagnostics", [])]
        missing_selected = [paper_id for paper_id in selected_ids if paper_id not in paper_by_id]
        if missing_selected:
            diagnostics.append(
                "Search handoff references missing selected papers: "
                + ", ".join(missing_selected)
            )
        for row in data.get("responses", []):
            if not isinstance(row, Mapping):
                continue
            paper_ids = [str(item) for item in row.get("paper_ids", [])]
            missing = [paper_id for paper_id in paper_ids if paper_id not in paper_by_id]
            if missing:
                diagnostics.append(
                    "Search handoff response references missing papers: "
                    + ", ".join(missing)
                )
            responses.append(
                SearchResponse(
                    source=str(row.get("source") or "unknown"),
                    query=str(row.get("query") or ""),
                    papers=[paper_by_id[paper_id] for paper_id in paper_ids if paper_id in paper_by_id],
                    status=str(row.get("status") or "ok"),
                    message=str(row.get("message") or ""),
                )
            )
            response_rounds.append(row.get("round", 1))
        return cls(
            status=status,  # type: ignore[arg-type]
            responses=tuple(responses),
            response_rounds=tuple(response_rounds),
            papers=papers,
            diagnostics=tuple(diagnostics),
            selected_papers=tuple(
                paper_by_id[paper_id]
                for paper_id in selected_ids
                if paper_id in paper_by_id
            )
            if "selected_paper_ids" in data
            else papers,
            selection_rows=tuple(
                dict(row) for row in data.get("selection", []) if isinstance(row, Mapping)
            ),
            coverage_report=(
                dict(data.get("coverage"))
                if isinstance(data.get("coverage"), Mapping)
                else {}
            ),
        )


def provided_materials_result(records: Iterable[DocumentRecord]) -> SearchResult:
    """Read-only compatibility projection, never a persisted search result."""
    papers = tuple(Paper(
        # Storage/chunk IDs are not a new bibliographic identity when the
        # original connector identity was retained with the document.
        id=record.metadata.get("paper_id") or record.document_id,
        title=record.title, authors=record.authors,
        abstract=record.abstract, url=record.url or "",
        published=record.published, source=record.source,
        source_id=record.source_id or record.document_id, doi=record.doi,
        bibliographic_notes=list(record.metadata.get("bibliographic_notes", [])),
    ) for record in records)
    return SearchResult(
        status="completed" if papers else "empty",
        responses=(), papers=papers, selected_papers=papers,
        diagnostics=("Search was not run; sources are supplied documents.",),
        coverage_report={"status": "provided_materials", "source_count": len(papers)},
    )


def search_sources(
    request: SearchRequest,
    *,
    registry: SearchProviderRegistry,
    emit: Callable[[str], None] | None = None,
) -> SearchResult:
    """Run the requested provider/query pairs without writing run artifacts.

    Provider construction and calls are isolated per pair. A source failure is
    retained as a failed response instead of being mistaken for an empty
    successful search. Successful responses remain usable when another source
    is unavailable.
    """
    responses: list[SearchResponse] = []
    papers: list[Paper] = []
    diagnostics: list[str] = []

    stop = False
    for provider_name in request.providers:
        rate_limited: SearchResponse | None = None
        for query in request.queries:
            if emit:
                emit(f"Searching {provider_name}: {query}")
            response = SearchResponse(
                source=provider_name, query=query, papers=[], status="rate_limited",
                message=f"Skipped live query after this provider returned HTTP 429 in the current batch: {rate_limited.message}",
            ) if rate_limited is not None else _run_provider(
                registry,
                provider_name,
                SearchQuery(
                    query=query,
                    max_results=request.max_results_per_query,
                    filters=dict(request.filters),
                ),
            )
            if response.status == "rate_limited" and rate_limited is None:
                rate_limited = response
            title = _normalized_title(query) if request.filters.get('match_title') else ()
            if title and _response_succeeded(response):
                response = replace(response, papers=[paper for paper in response.papers
                    if _normalized_title(paper.title) == title])
            response = _apply_optional_cache(
                response,
                query=query,
                provider_name=provider_name,
                request=request,
            )
            responses.append(response)
            if emit:
                emit(f"{provider_name}: {response.status}; {len(response.papers)} papers returned.")
            if _response_succeeded(response):
                papers.extend(response.papers)
            elif response.message:
                diagnostics.append(response.message)
            if request.stop_after_papers is not None:
                unique_paper_ids = {paper.id for paper in papers}
                if len(unique_paper_ids) >= request.stop_after_papers:
                    stop = True
                    break
        if stop:
            break

    return SearchResult(
        status=_result_status(responses, papers),
        responses=tuple(responses),
        papers=tuple(papers),
        diagnostics=tuple(diagnostics),
    )


def _normalized_title(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize('NFKC', text).casefold()
    return tuple(re.findall(r'[^\W_]+', normalized))


def _apply_optional_cache(
    response: SearchResponse,
    *,
    query: str,
    provider_name: str,
    request: SearchRequest,
) -> SearchResponse:
    """Persist successful metadata and recover cached metadata when enabled.

    Cache use is opt-in through ``cache_dir`` and ``cache_enabled``.  A cache
    miss never changes a provider failure, while a cache hit keeps the live
    error in the response message and marks the result as ``cached`` so a
    report can distinguish it from a fresh provider response.
    """

    if not request.cache_enabled or request.cache_dir is None:
        return response
    if request.filters.get('match_title'):
        query = f'{query} [match:title]'
    scope = temporal_scope(request.filters)
    if scope is not None:
        # Cache APIs already key by query; scoped entries must not replace or
        # recover unrestricted metadata (or another window).
        query = f"{query} [publication_year:{scope['start_year']}-{scope['end_year']}]"
    if _response_succeeded(response) and response.papers:
        try:
            (request.cache_put or put_cache)(
                query,
                provider_name,
                request.max_results_per_query,
                [paper.to_row() for paper in response.papers],
                cache_dir=request.cache_dir,
            )
        except (OSError, TypeError, ValueError):
            # Search is still valid; cache persistence is an optimization.
            pass
        return response
    if _response_succeeded(response):
        return response
    try:
        cached_rows = (request.cache_get or get_cached)(
            query,
            provider_name,
            request.max_results_per_query,
            cache_dir=request.cache_dir,
        )
    except (OSError, TypeError, ValueError):
        cached_rows = None
    if not cached_rows:
        return response
    cached_papers: list[Paper] = []
    for row in cached_rows:
        if not isinstance(row, Mapping):
            continue
        try:
            cached_papers.append(Paper.from_row(dict(row)))
        except (KeyError, TypeError, ValueError):
            continue
    if not cached_papers:
        return response
    return SearchResponse(
        source=response.source,
        query=response.query,
        papers=cached_papers,
        status="cached",
        message=(
            f"Live provider failed ({response.message or 'unknown error'}); "
            "using cached metadata."
        ),
    )


def run_search_capability(
    *,
    context: CapabilityContext,
    request: SearchRequest,
    registry: SearchProviderRegistry,
    selection_policy: SearchSelectionPolicy | None = None,
    excluded_paper_keys: frozenset[str] = frozenset(),
    linked_records: tuple[DocumentRecord, ...] = (),
    emit: Callable[[str], None] | None = None,
    budget_ledger: BudgetLedger | None = None,
    session_id: str = '',
) -> CapabilityResult:
    """Persist one explicit search handoff for a controller-managed attempt.

    With a selection policy, execute bounded new metadata queries from the
    existing coverage report. This is search-stage expansion, not reading or
    semantic evidence verification. Failures and every provider response stay
    visible; repeated queries and rounds without new paper identities stop.
    """

    attempt = context.attempt.attempt_id

    def search_round(value: SearchRequest, number: int) -> SearchResult:
        return run_source_operation(budget_ledger, f'source-search:{attempt}:{number}',
            len(value.queries) * len(value.providers), lambda: search_sources(value, registry=registry, emit=emit),
            purpose='research search', attempt_id=attempt, session_id=session_id)

    result = search_round(request, 1)
    if linked_records:
        supplied = provided_materials_result(linked_records)
        candidates = tuple(replace(paper, abstract=str(record.metadata.get("parent_quote") or record.abstract))
                           for paper, record in zip(supplied.papers, linked_records, strict=True))
        response = SearchResponse(source="supporting_material", query="Linked references in acquired sources",
            papers=list(candidates), status="ok",
            message="Locally supplied linked references with parent quotations; no provider request or original-page acquisition.")
        result = replace(result, papers=(*result.papers, *candidates),
            responses=(*result.responses, response),
            status=_result_status([*result.responses, response], [*result.papers, *candidates]))
    if selection_policy is not None:
        from simple_ar.research.evidence.retrieval import paper_identity_key

        policy = selection_policy
        result = replace(result, response_rounds=(1,) * len(result.responses))
        remaining_queries = policy.next_query_limit
        if budget_ledger is not None and (available := budget_ledger.remaining('source_operations')) is not None:
            remaining_queries = min(remaining_queries, max(0, int(available) // len(request.providers)))
        executed = {_query_key(response.query) for response in result.responses}
        identities: set[str] = set()
        round_index = 1
        last_round_status = result.status
        while True:
            current_identities = {paper_identity_key(paper) for paper in result.papers}
            new_candidates = len(current_identities - identities)
            result = select_search_result(result, policy=policy, filters=request.filters,
                                          excluded_paper_keys=excluded_paper_keys)
            if last_round_status == "failed":
                stop_reason = "providers_failed"
            elif not new_candidates:
                stop_reason = "no_new_candidates"
            elif request.stop_after_papers is not None and len({paper.id for paper in result.papers}) >= request.stop_after_papers:
                stop_reason = "paper_limit_reached"
            elif not policy.query_plan.auto_expansion:
                stop_reason = "query_expansion_disabled"
            elif round_index >= policy.query_plan.max_rounds:
                stop_reason = "round_limit_reached"
            elif remaining_queries <= 0:
                stop_reason = "follow_up_query_limit_reached"
            else:
                stop_reason = "no_new_queries"
                proposals = result.coverage_report.get("follow_up_queries", [])
                next_queries = _new_queries((row["query"] for row in proposals), executed)[:remaining_queries]
                if next_queries:
                    identities = current_identities
                    remaining_queries -= len(next_queries)
                    round_index += 1
                    if emit:
                        emit(f"Search round {round_index}: {len(next_queries)} new metadata queries (not source understanding).")
                    paper_limit = request.stop_after_papers
                    if paper_limit is not None:
                        paper_limit -= len({paper.id for paper in result.papers})
                    followup = search_round(
                        replace(request, queries=tuple(next_queries), stop_after_papers=paper_limit),
                        round_index,
                    )
                    last_round_status = followup.status
                    executed.update(_query_key(response.query) for response in followup.responses)
                    responses = (*result.responses, *followup.responses)
                    papers = (*result.papers, *followup.papers)
                    result = replace(result,
                        responses=responses,
                        response_rounds=result.response_rounds + (round_index,) * len(followup.responses),
                        papers=papers,
                        diagnostics=(*result.diagnostics, *followup.diagnostics),
                        status=_result_status(list(responses), list(papers)),
                    )
                    # Generated facet queries need their actual facet in the
                    # same selection policy; do not create a second plan artifact.
                    specs = {_query_key(row["query"]): row for row in proposals}
                    additional = _new_queries(next_queries, {_query_key(query) for query in policy.query_plan.queries})
                    policy = replace(policy, query_plan=replace(policy.query_plan,
                        queries=[*policy.query_plan.queries, *additional],
                        query_specs=[*policy.query_plan.query_specs, *[specs[_query_key(query)] for query in next_queries]],
                    ))
                    continue
            result = replace(result, coverage_report={**result.coverage_report,
                "scope": "search_metadata_only",
                "semantic_verification": "not_performed",
                "stop_reason": stop_reason,
            })
            if emit:
                emit(f"Search stopped: {stop_reason}; {round_index} round(s). Metadata coverage is not source understanding.")
            break
    elif (scope := temporal_scope(request.filters)) is not None:
        # Reading-driven followups do not rank metadata or expand again. They
        # still obey the original eligibility constraint before document ingest.
        from simple_ar.research.evidence.retrieval import publication_scope_exclusion
        rows, eligible = [], []
        for paper in result.papers:
            reason = publication_scope_exclusion(paper, scope)
            if reason is None:
                eligible.append(paper)
            rows.append({"paper_id": paper.id, "published": paper.published,
                         "decision": "discard" if reason else "keep",
                         "reason": reason or "within_temporal_scope"})
        result = replace(result, selected_papers=tuple(eligible), selection_rows=tuple(rows))
    if selection_policy is not None and request.llm_client is not None:
        from simple_ar.research.evidence.screening import screen_papers_with_llm
        # Save discovery before any paid judgment: a failed selection must not
        # erase provider responses, and retries can use the existing query cache.
        context.store.write_json("search_result.json", result.to_handoff_dict(),
            kind="search_result", schema="search_handoff.v1", producer="research.search")
        pool = select_search_result(result, policy=replace(selection_policy,
            max_documents=max(1, len(result.papers))), filters=request.filters,
            excluded_paper_keys=excluded_paper_keys, include_low_relevance=True)
        assessments: list[dict[str, Any]] = []
        settings = {**request.screening_config,
                    "read_screening_max_shortlist": selection_policy.max_documents,
                    "read_screening_min_shortlist": 0}
        decisions = screen_papers_with_llm(request.llm_client,
            topic=selection_policy.topic, problem_markdown=request.problem_markdown,
            research_plan_json=request.research_plan_json, config=settings,
            papers=[p.to_row() for p in pool.selected_papers], emit=emit,
            question_assessments=assessments, metadata_pool=True)
        kept = {r["paper_id"] for r in decisions if r.get("decision") == "keep"}
        selected = tuple(p for p in pool.selected_papers if p.id in kept)
        rows = tuple({**r, "decision": "keep" if r.get("paper_id") in kept else "discard"}
                     if r.get("decision") == "keep" else r for r in pool.selection_rows)
        from simple_ar.research.evidence.coverage import build_coverage_report
        coverage = build_coverage_report(topic=selection_policy.topic,
            questions=list(selection_policy.questions), query_plan=selection_policy.query_plan,
            selection_rows=list(rows), retrieval_rows=result.coverage_report["retrieval"]["attempts"],
            max_documents=selection_policy.max_documents, next_query_limit=0)
        coverage.update(scope="search_metadata_only", semantic_verification="not_performed",
            semantic_selection={"decisions": decisions, "question_assessments": assessments,
                                "scope": "candidate_metadata_not_read_evidence"})
        result = replace(result, selected_papers=selected, selection_rows=rows, coverage_report=coverage)
    diagnostics = list(result.diagnostics)
    if emit:
        emit(f"Search selection: {len(result.selected_papers)} papers retained.")
    if result.status == "empty":
        diagnostics.append("Search returned no papers.")
    output = context.store.write_json(
        "search_result.json",
        result.to_handoff_dict(),
        kind="search_result",
        schema="search_handoff.v1",
        producer="research.search",
    )
    capability_status = {
        "completed": "completed",
        "partial": "partial",
        "empty": "partial",
        "failed": "failed",
    }[result.status]
    return CapabilityResult(
        status=capability_status,  # type: ignore[arg-type]
        artifacts=(output,),
        diagnostics=tuple(diagnostics),
        usage={
            "query_count": len({_query_key(response.query) for response in result.responses}),
            "round_count": max(result.response_rounds or ((1,) if result.responses else (0,))),
            "provider_count": len(request.providers),
            "response_count": len(result.responses),
            "paper_count": len(result.papers),
            "selected_paper_count": len(result.selected_papers),
            "selection_count": len(result.selection_rows),
        },
        provenance={
            "capability": "search",
            "result_schema": "search_handoff.v1",
        },
    )


def select_search_result(
    result: SearchResult,
    *,
    policy: SearchSelectionPolicy,
    filters: Mapping[str, object] | None = None,
    excluded_paper_keys: frozenset[str] = frozenset(),
    include_low_relevance: bool = False,
) -> SearchResult:
    """Apply the canonical retrieval policy while retaining raw responses.

    The former legacy Search stage already had this behavior, but the new
    capability path previously passed every raw paper straight to ingest.  A
    selected paper list and auditable coverage rows now travel in the same
    search handoff; no second stage or hidden workspace scan is introduced.
    """

    from simple_ar.research.evidence.coverage import build_coverage_report
    from simple_ar.research.evidence.retrieval import (
        RetrievalCandidate,
        paper_identity_key,
        select_retrieval_candidates,
    )

    filter_topic = (filters or {}).get("topic")
    scope_topic = filter_topic if isinstance(filter_topic, str) else policy.topic
    scope = temporal_scope(filters or {}, topic=scope_topic)
    query_numbers = {
        _query_key(query): index
        for index, query in enumerate(policy.query_plan.queries, start=1)
        if query.strip()
    }
    specs = {
        _query_key(str(row.get("query") or "")): row
        for row in policy.query_plan.query_specs
        if isinstance(row, Mapping) and str(row.get("query") or "").strip()
    }
    candidates: list[RetrievalCandidate] = []
    excluded_rows = []
    retrieval_rows: list[dict[str, Any]] = []
    for response_index, response in enumerate(result.responses, start=1):
        query = response.query.strip()
        query_spec = specs.get(_query_key(query), {})
        query_index = query_numbers.get(_query_key(query), response_index)
        round_index = result.response_rounds[response_index - 1] if result.response_rounds else 1
        retrieval_rows.append(
            {
                "schema_version": "retrieval_round.v1",
                "round": round_index,
                "query_index": query_index,
                "query": query,
                "source": response.source,
                "status": response.status,
                "returned": len(response.papers),
                "message": response.message,
            }
        )
        if not _response_succeeded(response):
            continue
        facet = str(query_spec.get("facet") or "").strip()
        for paper in response.papers:
            if paper_identity_key(paper) in excluded_paper_keys:
                excluded_rows.append({"paper_id": paper.id, "source": response.source,
                                      "query": query, "decision": "discard",
                                      "reason": "already_retained_source"})
                continue
            candidates.append(
                RetrievalCandidate(
                    paper=paper,
                    source=response.source,
                    query=query,
                    query_index=query_index,
                    round_index=round_index,
                    facet=facet,
                    returned_source=response.source,
                )
            )
    selected, selection_rows = select_retrieval_candidates(
        candidates,
        max_documents=policy.max_documents,
        negative_terms=list(policy.query_plan.negative_terms),
        priority_facets=list(policy.query_plan.required_facets),
        temporal_scope=scope,
        include_low_relevance=include_low_relevance,
    )
    coverage = build_coverage_report(
        topic=policy.topic,
        questions=list(policy.questions),
        query_plan=policy.query_plan,
        selection_rows=selection_rows,
        retrieval_rows=retrieval_rows,
        max_documents=policy.max_documents,
        next_query_limit=policy.next_query_limit,
    )
    coverage.update(scope="search_metadata_only", semantic_verification="not_performed")
    coverage["retrieval"]["attempts"] = retrieval_rows
    return replace(
        result,
        selected_papers=tuple(selected),
        selection_rows=tuple([*excluded_rows, *selection_rows]),
        coverage_report=coverage,
    )


def _query_key(query: str) -> str:
    return " ".join(query.split()).casefold()


def _new_queries(queries: Iterable[str], executed: set[str]) -> list[str]:
    seen = set(executed)
    result = []
    for query in queries:
        normalized = " ".join(query.split())
        key = _query_key(normalized)
        if key and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def _run_provider(
    registry: SearchProviderRegistry,
    provider_name: str,
    request: SearchQuery,
) -> SearchResponse:
    try:
        connector = registry.resolve(provider_name)
        response = connector.search(request)
        if not isinstance(response, SearchResponse):
            raise TypeError(
                f"provider returned {type(response).__name__}; expected SearchResponse"
            )
        return response
    except Exception as exc:
        # Inspect the transport cause, not provider-specific error wording.
        # Query changes cannot overcome a provider/IP quota within this batch.
        cause: BaseException | None = exc
        seen: set[int] = set()
        status = "failed"
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            status_code = (cause.code if isinstance(cause, HTTPError)
                           else getattr(getattr(cause, "response", None), "status_code", None))
            if status_code == 429:
                status = "rate_limited"
                break
            cause = cause.__cause__ or cause.__context__
        message = (
            f"Search provider {provider_name!r} failed for query {request.query!r}: "
            f"{type(exc).__name__}: {exc}"
        )
        return SearchResponse(
            source=provider_name,
            query=request.query,
            papers=[],
            status=status,
            message=message,
        )


def _response_succeeded(response: SearchResponse) -> bool:
    return response.status.strip().lower() not in {"failed", "error", "blocked", "rate_limited"}


def _result_status(
    responses: list[SearchResponse],
    papers: list[Paper],
) -> SearchStatus:
    if not responses or all(not _response_succeeded(response) for response in responses):
        return "failed"
    if papers:
        if any(not _response_succeeded(response) for response in responses):
            return "partial"
        if any(response.status.strip().lower() in {"cached", "partial"} for response in responses):
            return "partial"
        return "completed"
    return "partial" if any(not _response_succeeded(response) for response in responses) else "empty"


__all__ = [
    "SearchRequest",
    "SearchSelectionPolicy",
    "SearchResult",
    "SearchStatus",
    "run_search_capability",
    "select_search_result",
    "search_sources",
]
