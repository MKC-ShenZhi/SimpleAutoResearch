"""Read-only historical research-session results.

New sessions are created and advanced by research_application. This module
does not own a second literature-to-experiment workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from simple_ar.core import (
    ArtifactRef,
    AttemptManifest,
    CapabilityRegistry,
    DecisionRecord,
    SessionController,
)
from simple_ar.research.documents.ingest import DocumentBundle
from simple_ar.research.evidence.reader import ReadResult
from simple_ar.research.analysis import AnalysisHandoff
from simple_ar.research.design import ResearchDesignResult
from simple_ar.research.planning.capability import ResearchPlanResult
from simple_ar.research.sources import SearchResult
from simple_ar.research.synthesis import SynthesisResult
from simple_ar.research.brief import ResearchBriefResult
from simple_ar.result_analysis.schema import AnalysisResult


@dataclass(frozen=True, slots=True)
class ResearchSessionResult:
    """Structured outputs from one full small research session."""

    session_root: Path
    plan: ResearchPlanResult
    search: SearchResult
    documents: DocumentBundle
    brief: SynthesisResult
    brief_result: ResearchBriefResult
    brief_ref: ArtifactRef
    execution: Mapping[str, Any]
    analysis: AnalysisResult
    execution_ref: ArtifactRef
    analysis_ref: ArtifactRef
    attempts: tuple[AttemptManifest, ...]
    decisions: tuple[DecisionRecord, ...]
    design: ResearchDesignResult | None = None
    design_ref: ArtifactRef | None = None

    @property
    def status(self) -> str:
        execution_status = str(self.execution.get("status") or "unknown")
        if execution_status == "passed" and self.analysis.status == "passed":
            return "ready_for_report"
        if self.analysis.status == "passed":
            return "partial"
        return self.analysis.status

    @property
    def report_ready(self) -> bool:
        """Whether this session has the evidence required for a formal report."""

        return self.status == "ready_for_report"

    @property
    def next_capability(self) -> str | None:
        """Return the explicit next handoff recorded by the session."""

        return self.decisions[-1].next_capability if self.decisions else None


class ResearchSessionError(RuntimeError):
    """Raised when historical session outputs cannot be read."""


def _attempt_sequence(attempt_id: str) -> tuple[int, str]:
    suffix = attempt_id.rsplit("-", 1)[-1]
    return (int(suffix), attempt_id) if suffix.isdigit() else (-1, attempt_id)


def _latest_attempt_id(
    attempts: tuple[AttemptManifest, ...],
    *,
    capability: str,
    parent_attempt_id: str | None = None,
) -> str:
    matches = tuple(
        item
        for item in attempts
        if item.capability == capability
        and (
            parent_attempt_id is None
            or item.parent_attempt == parent_attempt_id
        )
    )
    if not matches:
        raise KeyError(f"No {capability!r} attempt is available.")
    return max(matches, key=lambda item: _attempt_sequence(item.attempt_id)).attempt_id


def load_research_session_result(
    session_root: str | Path,
) -> ResearchSessionResult:
    """Restore a standard research-to-analysis session without rerunning it.

    Restoration uses only the session manifest and the declared typed outputs
    of the standard attempts.  It deliberately does not scan for a best
    artifact, infer missing stages, or register executable handlers; callers
    can inspect and project historical evidence without mutating the session.
    """

    root = Path(session_root)
    try:
        controller = SessionController.load(
            root,
            registry=CapabilityRegistry(),
        )
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        raise ResearchSessionError(
            f"Could not load research session {root}: {exc}"
        ) from exc

    try:
        attempts = controller.list_attempts()
        experiment_attempt_id = _latest_attempt_id(
            attempts,
            capability="experiment",
        )
        analysis_attempt_id = _latest_attempt_id(
            attempts,
            capability="analysis",
            parent_attempt_id=experiment_attempt_id,
        )
        plan_ref = controller.attempt_output_ref(
            "plan-001",
            kind="research_plan",
            schema="research_plan.v1",
        )
        plan = ResearchPlanResult.from_handoff_dict(controller.store.read_json(plan_ref))

        search_ref = controller.attempt_output_ref(
            "search-001",
            kind="search_result",
            schema="search_handoff.v1",
        )
        search = SearchResult.from_handoff_dict(controller.store.read_json(search_ref))

        document_ref = controller.attempt_output_ref(
            "document-001",
            kind="document_bundle",
            schema="document_bundle.v1",
        )
        documents = DocumentBundle.from_handoff_dict(
            controller.store.read_json(document_ref)
        )

        # New sessions expose Read and Synthesis as separate attempts. Keep
        # the old aggregate handoff readable so historical sessions remain
        # usable after this migration.
        if any(item.attempt_id == "read-001" for item in attempts):
            read_ref = controller.attempt_output_ref(
                "read-001",
                kind="read_result",
                schema="read_result.v1",
            )
            read_payload = controller.store.read_json(read_ref)
            if not isinstance(read_payload, Mapping):
                raise ValueError("Read handoff must be a JSON object.")
            read_result = ReadResult.from_handoff_dict(
                read_payload,
                bundle=documents,
            )
            brief_ref = controller.attempt_output_ref(
                "synthesize-001",
                kind="synthesis_result",
                schema="synthesis_result.v1",
            )
            synthesis_payload = controller.store.read_json(brief_ref)
            if not isinstance(synthesis_payload, Mapping):
                raise ValueError("Synthesis handoff must be a JSON object.")
            synthesis = SynthesisResult.from_handoff_dict(synthesis_payload)
            brief_result = ResearchBriefResult.from_parts(read_result, synthesis)
        else:
            brief_ref = controller.attempt_output_ref(
                "brief-001",
                kind="research_brief",
                schema="research_brief.v1",
            )
            brief_payload = controller.store.read_json(brief_ref)
            if not isinstance(brief_payload, Mapping):
                raise ValueError("Research brief handoff must be a JSON object.")
            brief_result = ResearchBriefResult.from_handoff_dict(
                brief_payload,
                bundle=documents,
            )
        if brief_result.synthesis is None:
            raise ValueError("Research brief handoff has no synthesis result.")

        design: ResearchDesignResult | None = None
        design_ref: ArtifactRef | None = None
        if any(item.attempt_id == "design-001" for item in attempts):
            design_ref = controller.attempt_output_ref(
                "design-001",
                kind="research_design",
                schema="research_design.v1",
            )
            design_payload = controller.store.read_json(design_ref)
            if not isinstance(design_payload, Mapping):
                raise ValueError("Research design handoff must be a JSON object.")
            design = ResearchDesignResult.from_handoff_dict(design_payload)
            if design.status != "ready" or design.contract is None:
                raise ValueError("Research design handoff is not executable.")

        execution_ref = controller.attempt_output_ref(
            experiment_attempt_id,
            kind="experiment_result",
            schema="canonical_results.2.5",
        )
        execution_payload = controller.store.read_json(execution_ref)
        if not isinstance(execution_payload, Mapping):
            raise ValueError("Experiment result must be a JSON object.")

        analysis_ref = controller.attempt_output_ref(
            analysis_attempt_id,
            kind="analysis_result",
            schema="analysis_handoff.v1",
        )
        analysis_payload = controller.store.read_json(analysis_ref)
        if not isinstance(analysis_payload, Mapping):
            raise ValueError("Analysis handoff must be a JSON object.")
        analysis = AnalysisHandoff.from_handoff_dict(analysis_payload).analysis
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ResearchSessionError(
            f"Research session {root} is missing a usable typed handoff: {exc}"
        ) from exc

    return ResearchSessionResult(
        session_root=root,
        plan=plan,
        search=search,
        documents=documents,
        brief=brief_result.synthesis,
        brief_result=brief_result,
        brief_ref=brief_ref,
        execution=dict(execution_payload),
        analysis=analysis,
        execution_ref=execution_ref,
        analysis_ref=analysis_ref,
        attempts=attempts,
        decisions=tuple(controller.manifest.decisions),
        design=design,
        design_ref=design_ref,
    )


__all__ = [
    "ResearchSessionError",
    "ResearchSessionResult",
    "load_research_session_result",
]
