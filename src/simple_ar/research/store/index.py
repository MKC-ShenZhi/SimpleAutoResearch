"""Retired persistent index entry point and historical cleanup locations.

Source queries rank canonical DocumentBundle chunks in memory. Keep the old
direct-import entry point explicit during migration; never recreate accelerators
or overwrite portable chunks, metadata, or an existing user's database.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from simple_ar.research.contracts import TextChunk


SQLITE_INDEX_NAME = "sqlite_fts.db"
DEFAULT_SHARED_INDEX_ROOT = ".simple_ar_cache/research_index"


def write_research_index(
    *,
    index_dir: Path,
    chunks: list[TextChunk],
    backend: str,
    run_id: str | None = None,
    shared_root: str | Path | None = None,
) -> dict[str, Any]:
    """Reject retired index production before any filesystem or backend access."""
    raise RuntimeError(
        "write_research_index is retired: persistent SQLite FTS/LanceDB stores "
        "were not used by production source queries. Retain documents/chunks "
        "with DocumentBundle.to_handoff_dict() and query retained chunks with "
        "rank_source_chunks(); existing index files remain available for historical cleanup."
    )
