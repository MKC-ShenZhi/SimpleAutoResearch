"""Shared service container and error types for research task execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

if TYPE_CHECKING:
    from simple_ar.research.sources.registry import SearchProviderRegistry


class ResearchApplicationError(RuntimeError):
    """Raised when a session cannot advance without an explicit decision."""


@dataclass(frozen=True, slots=True)
class ResearchApplicationServices:
    """Concrete dependencies and bounded settings for one application run."""

    llm_client: Any | None = field(default=None, repr=False, compare=False)
    feasibility_llm_client: Any | None = field(default=None, repr=False, compare=False)
    search_registry: SearchProviderRegistry | None = field(
        default=None, repr=False, compare=False
    )
    max_results: int = 10
    max_chunks: int | None = 300
    idea_limit: int = 3
    cache_dir: Path | None = None
    extraction_dir: Path | None = None
    input_base_dir: Path | None = None
    config: Mapping[str, object] = field(default_factory=dict)
    budget_limits: Mapping[str, int | float | None] = field(
        default_factory=lambda: {"llm_requests": None, "total_tokens": None}
    )
    # None leaves new native-task sizing to its owner; legacy stages resolve 16.
    max_attempts: int | None = None
    max_no_progress: int = 3
    message_callback: Callable[[str], None] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.max_results < 1 or self.idea_limit < 1:
            raise ValueError("Research application numeric settings must be positive.")
        if self.max_chunks is not None and self.max_chunks < 1:
            raise ValueError("max_chunks must be positive when provided.")
        if (self.max_attempts is not None and (
                type(self.max_attempts) is not int or self.max_attempts < 1)) or self.max_no_progress < 1:
            raise ValueError("Research application budgets must be positive.")
        for name in ("cache_dir", "extraction_dir", "input_base_dir"):
            value = getattr(self, name)
            object.__setattr__(self, name, Path(value) if value is not None else None)
        object.__setattr__(self, "config", dict(self.config))
        object.__setattr__(self, "budget_limits", dict(self.budget_limits))
