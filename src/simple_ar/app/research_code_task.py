"""Read historical segmented CodeTask sessions without executing them."""

from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from simple_ar.core import ArtifactRef, AttemptManifest, CapabilityRegistry, DecisionRecord, SessionController
from simple_ar.research.analysis import AnalysisHandoff
from simple_ar.research.design import ResearchDesignResult
from simple_ar.research.synthesis import SynthesisResult
from simple_ar.result_analysis.schema import AnalysisResult


@dataclass(frozen=True, slots=True)
class ResearchCodeTaskSessionResult:
    """Persisted code-task execution and result-analysis outputs."""

    topic: str
    session_root: Path
    synthesis: SynthesisResult
    execution: Mapping[str, Any]
    analysis: AnalysisResult
    source_ref: ArtifactRef
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
            return "completed"
        if self.analysis.status == "passed":
            return "partial"
        return self.analysis.status

    @property
    def execution_path(self) -> Path:
        return self.session_root / self.execution_ref.path

    @property
    def analysis_path(self) -> Path:
        return self.session_root / self.analysis_ref.path


class ResearchCodeTaskSessionError(RuntimeError):
    """Raised when historical CodeTask evidence cannot be read."""


def load_research_code_task_session_result(
    session_root: str | Path,
) -> ResearchCodeTaskSessionResult:
    """Restore one persisted Code-Task session without executing it.

    Restoration accepts only the session's declared synthesis input and the
    typed execution/analysis handoffs. It does not scan for an alternative
    result, infer a missing stage, or register executable handlers.
    """

    root = Path(session_root)
    try:
        controller = SessionController.load(
            root,
            registry=CapabilityRegistry(),
        )
        attempts = controller.list_attempts()
        experiment_attempt = next(
            (item for item in attempts if item.attempt_id == "experiment-001"), None
        )
        if experiment_attempt is None:
            raise KeyError("Unknown attempt: experiment-001")
        if len(experiment_attempt.inputs) not in {1, 2}:
            raise ValueError(
                "Code-task experiment must declare one synthesis input and an optional design input."
            )
        source_ref = experiment_attempt.inputs[0]
        source_payload = controller.store.read_json(source_ref)
        if not isinstance(source_payload, Mapping):
            raise ValueError("Code-task synthesis input must be a JSON object.")
        synthesis_payload = source_payload.get("synthesis")
        if not isinstance(synthesis_payload, Mapping):
            raise ValueError("Code-task synthesis input has no synthesis handoff.")
        synthesis = SynthesisResult.from_handoff_dict(synthesis_payload)

        design: ResearchDesignResult | None = None
        design_ref: ArtifactRef | None = None
        if len(experiment_attempt.inputs) == 2:
            design_ref = experiment_attempt.inputs[1]
            design_payload = controller.store.read_json(design_ref)
            if not isinstance(design_payload, Mapping):
                raise ValueError("Code-task design input must be a JSON object.")
            design = ResearchDesignResult.from_handoff_dict(design_payload)
            if design.status != "ready" or design.contract is None:
                raise ValueError("Code-task design handoff is not executable.")

        execution_ref = controller.attempt_output_ref(
            "experiment-001",
            kind="experiment_result",
            schema="canonical_results.2.5",
        )
        execution_payload = controller.store.read_json(execution_ref)
        if not isinstance(execution_payload, Mapping):
            raise ValueError("Code-task execution result must be a JSON object.")

        analysis_ref = controller.attempt_output_ref(
            "analysis-001",
            kind="analysis_result",
            schema="analysis_handoff.v1",
        )
        analysis_payload = controller.store.read_json(analysis_ref)
        if not isinstance(analysis_payload, Mapping):
            raise ValueError("Code-task analysis handoff must be a JSON object.")
        analysis_handoff = AnalysisHandoff.from_handoff_dict(analysis_payload)
        if analysis_handoff.execution_ref != execution_ref:
            raise ValueError(
                "Code-task analysis handoff does not reference experiment-001 output."
            )
    except (FileNotFoundError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ResearchCodeTaskSessionError(
            f"Could not restore code-task session {root}: {exc}"
        ) from exc

    return ResearchCodeTaskSessionResult(
        topic=controller.manifest.topic,
        session_root=root,
        synthesis=synthesis,
        execution=dict(execution_payload),
        analysis=analysis_handoff.analysis,
        source_ref=source_ref,
        execution_ref=execution_ref,
        analysis_ref=analysis_ref,
        attempts=attempts,
        decisions=tuple(controller.manifest.decisions),
        design=design,
        design_ref=design_ref,
    )
