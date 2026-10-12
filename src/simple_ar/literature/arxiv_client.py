from __future__ import annotations

import math
import threading
import time
from typing import Iterable

import arxiv
import requests

from simple_ar.literature.models import Paper, normalize_paper_id


class LiteratureSearchError(RuntimeError):
    """Raised when a literature provider cannot return usable metadata."""


class ArxivRateLimitError(LiteratureSearchError):
    """Raised when arXiv rejects a request because the client is rate limited."""


_RATE_LIMIT_THRESHOLD = 1
_RATE_LIMIT_COOLDOWN_SEC = 180.0
_rate_limit_lock = threading.Lock()
_rate_limit_count = 0
_circuit_open_until = 0.0


class _TimeoutSession(requests.Session):
    """Requests session that supplies a bounded default timeout."""

    def __init__(self, timeout_sec: float) -> None:
        super().__init__()
        self._timeout_sec = timeout_sec

    def request(self, method: str, url: str, **kwargs: object) -> requests.Response:
        kwargs.setdefault("timeout", self._timeout_sec)
        return super().request(method, url, **kwargs)


class ArxivSearchClient:
    """Small arXiv metadata search client.

    Args:
        page_size: Number of records requested per arXiv API page.
        delay_seconds: Delay between arXiv API requests.
        num_retries: Retry count delegated to the ``arxiv`` package.
        timeout_sec: Maximum time allowed for each HTTP request.
    """

    def __init__(
        self,
        *,
        page_size: int = 10,
        delay_seconds: float = 3.1,
        num_retries: int = 1,
        timeout_sec: float = 20.0,
    ) -> None:
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be greater than 0")
        self._client = arxiv.Client(
            page_size=page_size,
            delay_seconds=delay_seconds,
            num_retries=num_retries,
        )
        # arxiv.Client currently creates a requests.Session internally but does
        # not expose a timeout setting. Keep its pagination/rate-limit behavior
        # while injecting a bounded timeout at the session boundary.
        self._client._session = _TimeoutSession(timeout_sec)

    def search(self, query: str, *, max_results: int = 5) -> list[Paper]:
        """Search arXiv and return normalized paper metadata.

        Args:
            query: arXiv search query.
            max_results: Maximum number of papers to return.

        Returns:
            Deduplicated papers in arXiv relevance order.

        Raises:
            LiteratureSearchError: If the query is empty, the limit is invalid,
                or the provider request fails.
        """
        query = query.strip()
        if not query:
            raise LiteratureSearchError("arXiv query is empty")
        if max_results < 1:
            raise LiteratureSearchError("max_results must be at least 1")

        _raise_if_circuit_open()
        try:
            search = arxiv.Search(
                query=query,
                max_results=max_results,
                sort_by=arxiv.SortCriterion.Relevance,
                sort_order=arxiv.SortOrder.Descending,
            )
            papers = _dedupe_papers(_paper_from_result(result) for result in self._client.results(search))
            _record_success()
            return papers
        except Exception as exc:
            message = str(exc)
            if is_rate_limit_message(message):
                _record_rate_limit()
                raise ArxivRateLimitError(f"arXiv rate limit hit: {message}") from exc
            raise LiteratureSearchError(f"arXiv search failed: {message}") from exc


def _paper_from_result(result: arxiv.Result) -> Paper:
    """Convert an ``arxiv.Result`` into the project paper schema."""
    source_id = result.get_short_id()
    return Paper(
        id=normalize_paper_id(source_id),
        title=_clean_space(result.title),
        authors=[str(author) for author in result.authors],
        abstract=_clean_space(result.summary),
        # Atom uses an HTTP identifier; expose the canonical HTTPS acquisition
        # locator without upgrading arbitrary user or third-party URLs.
        url=f"https://arxiv.org/abs/{source_id}",
        published=result.published.date().isoformat() if result.published else None,
        categories=list(result.categories),
        source="arxiv",
        source_id=source_id,
        doi=result.doi,
    )


def _dedupe_papers(papers: Iterable[Paper]) -> list[Paper]:
    """Drop duplicate paper ids while preserving result order."""
    seen: set[str] = set()
    unique: list[Paper] = []
    for paper in papers:
        if paper.id in seen:
            continue
        seen.add(paper.id)
        unique.append(paper)
    return unique


def _clean_space(text: str) -> str:
    """Collapse whitespace from arXiv text fields."""
    return " ".join(text.split())


def is_rate_limit_message(message: str) -> bool:
    """Return whether an arXiv error message looks like an HTTP 429 response.

    Args:
        message: Provider or library exception message.

    Returns:
        ``True`` when the message indicates rate limiting.
    """
    normalized = message.lower()
    return "http 429" in normalized or "rate limit" in normalized or "too many requests" in normalized


def circuit_breaker_seconds_remaining() -> int:
    """Return the remaining arXiv circuit-breaker cooldown in seconds."""
    with _rate_limit_lock:
        remaining = max(0.0, _circuit_open_until - time.monotonic())
    return int(math.ceil(remaining))


def reset_rate_limit_circuit_for_tests() -> None:
    """Reset arXiv rate-limit state for deterministic tests."""
    global _rate_limit_count, _circuit_open_until
    with _rate_limit_lock:
        _rate_limit_count = 0
        _circuit_open_until = 0.0


def _raise_if_circuit_open() -> None:
    """Stop requests while the simple arXiv circuit breaker is open."""
    remaining = circuit_breaker_seconds_remaining()
    if remaining > 0:
        raise ArxivRateLimitError(
            f"arXiv circuit breaker open for {remaining}s after recent rate limits"
        )


def _record_success() -> None:
    """Close the simple circuit breaker after a successful arXiv request."""
    global _rate_limit_count, _circuit_open_until
    with _rate_limit_lock:
        _rate_limit_count = 0
        _circuit_open_until = 0.0


def _record_rate_limit() -> None:
    """Open the simple circuit breaker after repeated arXiv rate limits."""
    global _rate_limit_count, _circuit_open_until
    with _rate_limit_lock:
        _rate_limit_count += 1
        if _rate_limit_count >= _RATE_LIMIT_THRESHOLD:
            _circuit_open_until = time.monotonic() + _RATE_LIMIT_COOLDOWN_SEC
