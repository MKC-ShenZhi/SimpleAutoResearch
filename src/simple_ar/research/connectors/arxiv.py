from __future__ import annotations

import re

from simple_ar.literature.arxiv_client import ArxivSearchClient
from simple_ar.research.sources.base import SearchQuery, SearchResponse, temporal_scope


class ArxivConnector:
    """Research source connector backed by the existing arXiv client."""

    source_name = "arxiv"

    def __init__(self, client: ArxivSearchClient | None = None) -> None:
        self._client = client or ArxivSearchClient()

    def search(self, request: SearchQuery) -> SearchResponse:
        """Search arXiv and return a source-agnostic response."""
        scope = temporal_scope(request.filters)
        query = request.query
        # Plain provider-neutral keywords must not be left to implicit search
        # semantics. Keep explicit arXiv expressions intact for expert callers.
        if request.filters.get('match_title'):
            title = query.replace('"', '').replace('\\', '')
            query = f'ti:"{title}"'
        elif not re.search(r'\b(?:AND|OR|ANDNOT)\b|[\w]+:|["()]', query):
            query = " AND ".join(f"all:{term}" for term in query.split())
        if scope is not None:
            query = (f"({query}) AND submittedDate:[{scope['start_year']:04d}01010000 "
                     f"TO {scope['end_year']:04d}12312359]")
        papers = self._client.search(query, max_results=request.max_results)
        return SearchResponse(
            source=self.source_name,
            query=request.query,
            papers=papers,
            status="ok",
        )
