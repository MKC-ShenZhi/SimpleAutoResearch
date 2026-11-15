"""Small composed research-brief capability.

This module composes the existing Read and Synthesis boundaries.  It is a
local, deterministic handoff for a research brief; search, LLM prose, and
artifact persistence remain policies of their callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from simple_ar.research.evidence.reader import (
    ReadRequest,
    ReadResult,
    format_bundle_evidence_snippets,
    read_documents,
)
from simple_ar.research.synthesis import (
    SynthesisRequest,
    SynthesisResult,
    allowed_evidence_refs,
    synthesize_evidence,
)
from simple_ar.research.documents.ingest import DocumentBundle


ResearchBriefStatus = Literal["ready", "partial", "needs_review", "empty"]


@dataclass(frozen=True, slots=True)
class ResearchBriefRequest:
    """Inputs for one evidence-backed research brief.

    The default remains deterministic for library compatibility. A caller
    that wants model-assisted reading and synthesis must set ``use_llm`` and
    provide the shared client explicitly.
    """

    topic: str
    bundle: DocumentBundle
    document_ids: tuple[str, ...] | None = None
    paper_ids: tuple[str, ...] | None = None
    idea_limit: int = 3
    novelty_backend: str = "local"
    include_experiment_contract: bool = True
    use_llm: bool = False
    llm_client: Any | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.topic.strip():
            raise ValueError("ResearchBriefRequest.topic cannot be empty.")
        if self.idea_limit < 1:
            raise ValueError("idea_limit must be at least 1.")
        if not self.novelty_backend.strip():
            raise ValueError("novelty_backend cannot be empty.")
        if self.use_llm and self.llm_client is None:
            raise ValueError("ResearchBriefRequest.llm_client is required when use_llm is true.")


@dataclass(frozen=True, slots=True)
class ResearchBriefResult:
    """Read and synthesis outputs for one research-brief attempt."""

    status: ResearchBriefStatus
    read: ReadResult
    synthesis: SynthesisResult | None
    diagnostics: tuple[str, ...] = ()

    @classmethod
    def from_parts(
        cls,
        read: ReadResult,
        synthesis: SynthesisResult | None,
    ) -> "ResearchBriefResult":
        """Combine explicit Read and Synthesis results into a brief view.

        The aggregate is a compatibility view for callers that want one
        object.  The canonical session workflow persists Read and Synthesis
        as separate attempts; it does not need a hidden composite capability.
        """

        if synthesis is None:
            return cls(
                status="empty",
                read=read,
                synthesis=None,
                diagnostics=read.diagnostics,
            )
        diagnostics = tuple(dict.fromkeys((*read.diagnostics, *synthesis.diagnostics)))
        if synthesis.status == "needs_review":
            status: ResearchBriefStatus = "needs_review"
        elif read.status == "partial":
            status = "partial"
        else:
            status = "ready"
        return cls(
            status=status,
            read=read,
            synthesis=synthesis,
            diagnostics=diagnostics,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a compact handoff summary without copying source text."""
        return {
            "schema_version": "research_brief_result.v1",
            "status": self.status,
            "topic_document_count": len(self.read.bundle.records),
            "chunk_count": len(self.read.bundle.chunks),
            "paper_card_count": len(self.read.paper_cards),
            "idea_count": len(self.synthesis.ideas) if self.synthesis else 0,
            "has_experiment_contract": bool(
                self.synthesis and self.synthesis.experiment_contract
            ),
            "generation_mode": (
                self.synthesis.generation_mode if self.synthesis else "none"
            ),
            "diagnostics": list(self.diagnostics),
        }

    def to_handoff_dict(self, *, topic: str) -> dict[str, Any]:
        """Return structured evidence for an explicit downstream handoff.

        The compact ``to_dict()`` shape is useful for status displays.  A
        session handoff needs the cards and direction objects themselves, but
        it should not copy source chunk text into another artifact.  Source
        spans therefore retain identifiers and locations only.
        """
        read = self.read
        read_payload = read.to_handoff_dict()
        synthesis_payload: dict[str, Any] | None = None
        if self.synthesis is not None:
            synthesis_payload = self.synthesis.to_handoff_dict()
        return {
            "schema_version": "research_brief.v1",
            "topic": topic,
            "status": self.status,
            "read": read_payload,
            "synthesis": synthesis_payload,
            "diagnostics": list(self.diagnostics),
        }

    @classmethod
    def from_handoff_dict(
        cls,
        data: Mapping[str, Any],
        *,
        bundle: DocumentBundle,
    ) -> "ResearchBriefResult":
        """Restore a persisted brief while keeping source text in its bundle."""

        if str(data.get("schema_version") or "") != "research_brief.v1":
            raise ValueError("Expected a research_brief.v1 object.")
        status = str(data.get("status") or "")
        if status not in {"ready", "partial", "needs_review", "empty"}:
            raise ValueError(f"Unsupported research brief handoff status: {status!r}")
        read_payload = data.get("read")
        if not isinstance(read_payload, Mapping):
            raise ValueError("Research brief handoff is missing its read result.")
        synthesis_payload = data.get("synthesis")
        synthesis = (
            SynthesisResult.from_handoff_dict(synthesis_payload)
            if isinstance(synthesis_payload, Mapping)
            else None
        )
        return cls(
            status=status,  # type: ignore[arg-type]
            read=ReadResult.from_handoff_dict(read_payload, bundle=bundle),
            synthesis=synthesis,
            diagnostics=tuple(
                str(item) for item in data.get("diagnostics", [])
            ),
        )


def build_research_brief(request: ResearchBriefRequest) -> ResearchBriefResult:
    """Compose reading and evidence-to-direction synthesis.

    The function does not search or write files. Synthesis is deterministic by
    default; callers can explicitly set `use_llm` and provide the shared
    client to add grounded model prose. It never chooses a final research
    claim or a next workflow stage.
    """
    read = read_documents(
        ReadRequest(
            bundle=request.bundle,
            document_ids=request.document_ids,
            paper_ids=request.paper_ids,
            topic=request.topic,
            use_llm=request.use_llm,
            llm_client=request.llm_client,
        )
    )
    if read.status == "empty":
        return ResearchBriefResult.from_parts(read, None)

    synthesis = synthesize_evidence(
        SynthesisRequest(
            evidence_pack=evidence_pack_from_read(request.topic, read),
            idea_limit=request.idea_limit,
            novelty_backend=request.novelty_backend,
            include_experiment_contract=request.include_experiment_contract,
            use_llm=request.use_llm,
            llm_client=request.llm_client,
        )
    )
    return ResearchBriefResult.from_parts(read, synthesis)


def evidence_pack_from_read(
    topic: str,
    result: ReadResult,
    *,
    coverage: Mapping[str, Any] | None = None,
    source_plan: Mapping[str, Any] | None = None,
    execution_context: str | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Adapt a typed Read result to the minimal synthesis input shape.

    This is the shared domain handoff for Read and Synthesis. It preserves
    cards and source references, with bounded snippets for model context.
    """
    return {
        "schema_version": "evidence_pack.v1",
        "topic": topic,
        "source_plan": dict(source_plan or {}),
        "counts": {
            "documents": len(result.bundle.records),
            "chunks": len(result.bundle.chunks),
            "paper_cards": len(result.paper_cards),
            "claim_cards": len(result.claim_cards),
            "method_cards": len(result.method_cards),
            "dataset_cards": len(result.dataset_cards),
            "screening_decisions": len(result.screening_decisions),
            "paper_notes": len(result.paper_notes),
        },
        "coverage": dict(coverage or {"status": "unknown"}),
        "papers": [record.to_row() for record in result.bundle.records],
        "paper_cards": [card.to_row() for card in result.paper_cards],
        "claim_cards": [card.to_row() for card in result.claim_cards],
        "method_cards": [card.to_row() for card in result.method_cards],
        "dataset_cards": [card.to_row() for card in result.dataset_cards],
        "screening_decisions": [dict(row) for row in result.screening_decisions],
        "question_assessments": [dict(row) for row in result.question_assessments],
        "paper_notes": [_current_paper_note(note) for note in result.paper_notes],
        "notes_markdown": result.notes_markdown,
        "evidence_refs": [chunk.chunk_id for chunk in result.bundle.chunks],
        "evidence_snippets": _evidence_snippets(result),
        "limitations": list(result.diagnostics),
        "execution_context": _execution_context_payload(execution_context),
    }


def assessment_evidence_from_read(result: ReadResult) -> dict[str, Any]:
    """Project the same reading evidence to every idea-assessment consumer."""
    references = {"evidence_refs": [chunk.chunk_id for chunk in result.bundle.chunks]}
    for key, rows in (("papers", result.bundle.records), ("paper_cards", result.paper_cards),
                      ("claim_cards", result.claim_cards), ("method_cards", result.method_cards),
                      ("dataset_cards", result.dataset_cards), ("code_links", result.code_links)):
        references[key] = [row.to_row() for row in rows]
    return {
        "available_evidence_refs": tuple(sorted(allowed_evidence_refs(references))),
        "evidence_chunks": tuple(result.bundle.chunks),
        "evidence_cards": (*result.claim_cards, *result.method_cards),
    }


def _current_paper_note(note: Mapping[str, Any]) -> dict[str, Any]:
    """Keep correction provenance, not superseded interpretations, in synthesis."""
    current = dict(note)
    followup = current.get("reading_followup")
    if isinstance(followup, Mapping):
        current["reading_followup"] = {key: value for key, value in followup.items() if key != "prior_note"}
    return current


def _evidence_snippets(result: ReadResult, *, max_chunks: int = 12, max_chars: int = 900) -> str:
    """Render bounded source snippets for LLM synthesis provenance.

    The document bundle remains the source of truth.  This short prompt view
    replaces the old stage-wide retrieval lookup without copying an entire
    document into the synthesis handoff or allowing unbounded context growth.
    """

    return format_bundle_evidence_snippets(
        result.bundle,
        max_chunks=max_chunks,
        max_chars=max_chars,
    )


def _execution_context_payload(value: object) -> str | dict[str, Any]:
    """Normalize optional prepared-experiment constraints for synthesis."""

    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return str(value or "").strip()


__all__ = [
    "ResearchBriefRequest",
    "ResearchBriefResult",
    "ResearchBriefStatus",
    "build_research_brief",
    "evidence_pack_from_read",
]
