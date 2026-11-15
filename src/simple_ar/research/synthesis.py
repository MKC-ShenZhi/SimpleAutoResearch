"""Standalone evidence-to-direction synthesis boundary.

The existing evidence derivation functions remain the implementation.  This
module gives callers one small request/result boundary without taking over
LLM synthesis or persistence policy.  A caller can use it with the expanded
evidence pack already assembled by the pipeline, or with a small fixture.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from typing import Any, Callable, Literal, Mapping

from simple_ar.core.capabilities import CapabilityContext, CapabilityResult
from simple_ar.integrations.llm import LLMError
from simple_ar.research.contracts import (
    CLAIM_SCOPE_RULES,
    IdeaCandidate,
    NoveltyCheck,
    ResearchExperimentContract,
)
from simple_ar.research.evidence.derivation import (
    build_experiment_contract,
    build_gap_summary,
    build_idea_candidates,
    build_novelty_checks,
)
from simple_ar.research.prompts import SYNTHESIZE_SYSTEM, synthesize_user_prompt
from simple_ar.research.evidence.reader import reading_followup_context


SynthesisStatus = Literal["ready", "needs_review"]
COMPARISON_RELATIONS = {"agreement", "conditional_difference", "conflict", "not_comparable", "single_source"}


@dataclass(frozen=True, slots=True)
class SynthesisRequest:
    """Input for evidence synthesis.

    ``evidence_pack`` is the in-memory, expanded form produced by
    ``evidence_pack_from_read``. Historical compact packs contain references instead
    of card rows; callers must resolve those before using this boundary.
    ``use_llm`` is explicit so callers
    can keep a reproducible offline path without silently mistaking it for
    model-generated research prose.
    """

    evidence_pack: Mapping[str, Any]
    idea_limit: int = 3
    novelty_backend: str = "local"
    include_experiment_contract: bool = True
    use_llm: bool = False
    llm_client: Any | None = field(default=None, repr=False, compare=False)
    purpose: Literal["research", "evidence_review"] = "research"
    user_requests: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.idea_limit < 1:
            raise ValueError("idea_limit must be at least 1")
        if self.purpose not in {"research", "evidence_review"}:
            raise ValueError("purpose must be research or evidence_review")
        if not self.novelty_backend.strip():
            raise ValueError("novelty_backend must not be empty")
        if self.use_llm and self.llm_client is None:
            raise ValueError("SynthesisRequest.llm_client is required when use_llm is true.")


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    """Structured direction and experiment handoff from one evidence pack."""

    status: SynthesisStatus
    gap_summary: str
    ideas: tuple[IdeaCandidate, ...]
    novelty_checks: tuple[NoveltyCheck, ...]
    experiment_contract: ResearchExperimentContract | None = None
    synthesis_markdown: str = ""
    hypothesis_markdown: str = ""
    generation_mode: Literal["deterministic", "llm"] = "deterministic"
    diagnostics: tuple[str, ...] = ()
    execution_context: str = ""
    comparisons: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a compact JSON-serializable summary."""
        return {
            "schema_version": "synthesis_result.v1",
            "status": self.status,
            "idea_count": len(self.ideas),
            "novelty_check_count": len(self.novelty_checks),
            "has_experiment_contract": self.experiment_contract is not None,
            "generation_mode": self.generation_mode,
            "synthesis_character_count": len(self.synthesis_markdown),
            "hypothesis_character_count": len(self.hypothesis_markdown),
            "diagnostics": list(self.diagnostics),
            "execution_context_character_count": len(self.execution_context),
            "comparison_count": len(self.comparisons),
        }

    def to_handoff_dict(self) -> dict[str, Any]:
        """Return the bounded direction handoff for a downstream capability."""
        return {
            "schema_version": "synthesis_result.v1",
            "status": self.status,
            "gap_summary": self.gap_summary,
            "ideas": [idea.to_row() for idea in self.ideas],
            "novelty_checks": [check.to_row() for check in self.novelty_checks],
            "experiment_contract": (
                self.experiment_contract.to_row()
                if self.experiment_contract is not None
                else None
            ),
            "synthesis_markdown": self.synthesis_markdown,
            "hypothesis_markdown": self.hypothesis_markdown,
            "generation_mode": self.generation_mode,
            "diagnostics": list(self.diagnostics),
            "execution_context": self.execution_context,
            "comparisons": [dict(row) for row in self.comparisons],
        }

    def for_idea(self, idea_id: str) -> "SynthesisResult":
        """Return a handoff whose experiment contract follows one idea.

        Synthesis may expose several grounded ideas while the historical
        contract builder keeps the first one as the default.  Candidate
        execution must make that choice explicit; it must not silently run
        every candidate against the same hypothesis and proposed change.
        """

        wanted = idea_id.strip()
        if not wanted:
            raise ValueError("idea_id cannot be empty.")
        idea = next((item for item in self.ideas if item.idea_id == wanted), None)
        if idea is None:
            raise KeyError(f"Unknown synthesis idea: {wanted}")
        contract = self.experiment_contract
        if contract is None:
            return self
        selected_contract = replace(
            contract,
            contract_id=f"{contract.contract_id}/{idea.idea_id}",
            hypothesis=idea.hypothesis or contract.hypothesis,
            motivation_refs=list(idea.motivation_refs or contract.motivation_refs),
            baseline=(
                "; ".join(
                    dict.fromkeys(
                        item.strip()
                        for item in idea.required_baselines
                        if item.strip()
                    )
                )
                if idea.required_baselines
                else contract.baseline
            ),
            dataset=(
                idea.required_datasets[0]
                if idea.required_datasets
                else contract.dataset
            ),
            metrics=list(idea.metrics or contract.metrics),
            proposed_change=idea.proposed_change or contract.proposed_change,
            expected_outcome=idea.expected_outcome or contract.expected_outcome,
            risks=list(idea.risks or contract.risks),
        )
        return replace(self, experiment_contract=selected_contract)

    @classmethod
    def from_handoff_dict(cls, data: Mapping[str, Any]) -> "SynthesisResult":
        """Restore a ``synthesis_result.v1`` without calling an LLM."""

        if str(data.get("schema_version") or "") != "synthesis_result.v1":
            raise ValueError("Expected a synthesis_result.v1 object.")
        status = str(data.get("status") or "")
        if status not in {"ready", "needs_review"}:
            raise ValueError(f"Unsupported synthesis handoff status: {status!r}")
        contract_payload = data.get("experiment_contract")
        contract = (
            ResearchExperimentContract.from_row(contract_payload)
            if isinstance(contract_payload, Mapping)
            else None
        )
        return cls(
            status=status,  # type: ignore[arg-type]
            gap_summary=str(data.get("gap_summary") or ""),
            ideas=tuple(
                IdeaCandidate.from_row(row)
                for row in _mapping_rows(data.get("ideas"))
            ),
            novelty_checks=tuple(
                NoveltyCheck.from_row(row)
                for row in _mapping_rows(data.get("novelty_checks"))
            ),
            experiment_contract=contract,
            synthesis_markdown=str(data.get("synthesis_markdown") or ""),
            hypothesis_markdown=str(data.get("hypothesis_markdown") or ""),
            generation_mode=_generation_mode(data.get("generation_mode")),
            diagnostics=tuple(str(item) for item in data.get("diagnostics", [])),
            execution_context=str(data.get("execution_context") or ""),
            comparisons=tuple(dict(row) for row in _mapping_rows(data.get("comparisons"))),
        )


def _synthesize_deterministic_evidence(request: SynthesisRequest) -> SynthesisResult:
    """Derive bounded ideas, novelty-risk hints, and an experiment handoff.

    This helper is deliberately deterministic and does not call an LLM, write
    files, or choose a report claim.
    """
    pack = dict(request.evidence_pack)
    ideas = build_idea_candidates(pack, limit=request.idea_limit) if request.purpose == "research" else []
    novelty_checks = build_novelty_checks(
        ideas,
        pack,
        backend=request.novelty_backend,
    )
    experiment_contract = (
        build_experiment_contract(ideas, pack)
        if request.include_experiment_contract and request.purpose == "research"
        else None
    )

    diagnostics = _diagnostics(pack, ideas, require_ideas=request.purpose == "research")
    return SynthesisResult(
        status="ready" if not diagnostics else "needs_review",
        gap_summary=build_gap_summary(pack, require_execution=request.purpose == "research"),
        ideas=tuple(ideas),
        novelty_checks=tuple(novelty_checks),
        experiment_contract=experiment_contract,
        generation_mode="deterministic",
        diagnostics=tuple(diagnostics),
        execution_context=_execution_context_text(pack),
        synthesis_markdown=_evidence_notes_markdown(pack) if request.purpose == "evidence_review" else "",
    )


def synthesize_evidence(request: SynthesisRequest, *, trace: Callable[[str, Any], None] | None = None) -> SynthesisResult:
    """Synthesize evidence using the selected deterministic or LLM mode.

    The default remains deterministic. When ``request.use_llm`` is true, the
    existing structured derivation is retained and the shared LLM client adds
    grounded synthesis prose and may replace the deterministic idea candidates
    with a bounded structured list. A missing optional idea list preserves the
    older response shape; a malformed supplied list is an error rather than a
    silent fallback.
    """

    result = _synthesize_deterministic_evidence(request)
    if not request.use_llm:
        return result
    return _add_llm_synthesis(result, request, trace=trace)


def run_synthesis_capability(
    *,
    context: CapabilityContext,
    request: SynthesisRequest,
) -> CapabilityResult:
    """Persist one evidence-to-direction handoff.

    The caller supplies the expanded evidence pack. LLM prose and, when
    returned, structured candidates are generated only when the request
    explicitly carries a client; all candidate fields remain bounded by the
    supplied evidence identifiers and the existing handoff schema.
    """
    traces = []

    def save_trace(name: str, payload: Any) -> None:
        traces.append(context.store.write_json(f"{name}.json", payload,
            kind="synthesis_trace", schema="synthesis_trace.v1", producer="research.synthesis"))

    try:
        result = synthesize_evidence(request, trace=save_trace)
    except LLMError as exc:
        return CapabilityResult(status="failed", artifacts=tuple(traces), diagnostics=(str(exc),))
    output = context.store.write_json(
        "synthesis_result.json",
        result.to_handoff_dict(),
        kind="synthesis_result",
        schema="synthesis_result.v1",
        producer="research.synthesis",
    )
    return CapabilityResult(
        status="completed" if result.status == "ready" else "partial",
        artifacts=(output, *traces),
        diagnostics=result.diagnostics,
        usage={
            "ideas": len(result.ideas),
            "novelty_checks": len(result.novelty_checks),
        },
        provenance={
            "capability": "synthesis",
            "result_schema": "synthesis_result.v1",
            "mode": result.generation_mode,
            "model": str(getattr(request.llm_client, "model", ""))
            if request.use_llm
            else "",
        },
    )


def _add_llm_synthesis(
    result: SynthesisResult,
    request: SynthesisRequest,
    *, trace: Callable[[str, Any], None] | None = None,
) -> SynthesisResult:
    """Add grounded model prose to an already-derived structured result."""

    client = request.llm_client
    if client is None:
        raise LLMError("LLM synthesis was requested but no client was provided.")
    if not _has_evidence(request.evidence_pack):
        raise LLMError(
            "LLM synthesis requires at least one evidence card or source chunk; "
            "refusing deterministic fallback."
        )

    pack = dict(request.evidence_pack)
    cards = _bounded_pack_json(pack)
    excerpts = str(pack.get("evidence_snippets") or "")
    system = SYNTHESIZE_SYSTEM
    if request.purpose == "evidence_review":
        system = "Synthesize supplied source evidence for a review or a fixed-protocol reproduction. Distinguish source claims, reported observations, interpretation and unknowns. Never invent evidence."
        prompt = (
            "Return JSON with synthesis_markdown: a source-grounded synthesis addressing the user request. "
            "Preserve assumptions, actual reported empirical evidence and its conditions, disagreements, limitations and unresolved questions. "
            "Source measurements are prior work, not a local run. This task does not request innovative candidates or a new experiment protocol; "
            "do not return idea_candidates or a proposed hypothesis. Use the supplied source identifiers for provenance.\n\n"
            + "\n".join(CLAIM_SCOPE_RULES) + "\n\n"
            + "Structured source context:\n" + cards
            + "\n\nSource excerpts:\n" + excerpts
        )
    else:
        counts = pack.get("counts")
        prompt = synthesize_user_prompt("", cards, excerpts, json.dumps({
            "deterministic_idea_count": len(result.ideas),
        }, ensure_ascii=False), source_count=(int(counts["documents"])
            if isinstance(counts, Mapping) and counts.get("documents") is not None else None))
    if request.user_requests:
        prompt += ("\n\nOriginal user request and subsequent feedback (not source evidence):\n"
                   + json.dumps(request.user_requests, ensure_ascii=False))
    prompt += (
        "\n\nAlso return comparisons: a list of substantive, task-relevant comparisons, not one summary per paper. "
        "Each object has dimension (the actual question or property compared), relation "
        "(agreement, conditional_difference, conflict, not_comparable, or single_source), "
        "evidence_refs (exact supplied IDs), observations (what each source actually establishes), "
        "conditions (settings, definitions, units or assumptions affecting comparison), "
        "interpretation (why the evidence matters to the user), and unresolved_question. "
        "A difference in setting or metric is not a contradiction. Preserve counterevidence. "
        "Use single_source when only one independent source supports an assessment; this does not exclude "
        "comparisons of methods, controls or ablations actually measured within that source. Preserve the "
        "matched subset and its conditions without extending it to unmatched settings. Return [] when no comparison is supported. "
        "These are model interpretations, not independently verified facts. Do not introduce new studies or measurements."
    )
    prompt += "\nComparison response contract:\n" + json.dumps({
        "comparisons": {"type": "array", "item_fields": {
            "dimension": {"type": "string"},
            "relation": {"type": "string", "allowed_values": sorted(COMPARISON_RELATIONS)},
            "evidence_refs": {"type": "array", "items": "exact ID strings from allowed_motivation_refs; not invented note/section IDs"},
            "observations": {"type": "string or array of nonempty strings"},
            "conditions": {"type": "string or array of nonempty strings"},
            "interpretation": {"type": "string"}, "unresolved_question": {"type": "string"}}},
    }, ensure_ascii=False)
    response = client.ask_json(system, prompt, label="research-synthesis")
    for round_index in range(2):
        if trace is not None:
            trace(f"response-{round_index + 1}", response)
        try:
            if not isinstance(response, Mapping):
                raise LLMError("LLM synthesis response must be a JSON object.")
            synthesis_markdown = _required_text(response, "synthesis_markdown")
            comparisons = _parse_evidence_comparisons(response.get("comparisons"), pack)
            if request.purpose == "evidence_review":
                if response.get("idea_candidates"):
                    raise LLMError("Evidence-review synthesis must not generate research candidates.")
                hypothesis_markdown = ""
                llm_ideas = None
            else:
                hypothesis_markdown = _required_text(response, "hypothesis_markdown")
                llm_ideas = _parse_llm_idea_candidates(response, pack, limit=request.idea_limit)
            break
        except LLMError as exc:
            if trace is not None:
                trace(f"validation-{round_index + 1}", {"error": str(exc)})
            if round_index == 1:
                raise
            correction = {"validation_error": str(exc), "previous_response": response,
                          "allowed_motivation_refs": _selected_pack(pack)["allowed_motivation_refs"]}
            response = client.ask_json(system,
                prompt + "\n\nCorrect the rejected JSON once. Preserve grounded content; revise or omit an idea "
                "that lacks evidence, never replace its citation with an unrelated allowed ID. Return the complete object.\n"
                + json.dumps(correction, ensure_ascii=False, separators=(",", ":")),
                label="research-synthesis-correction")
    if llm_ideas is None:
        return replace(
            result,
            synthesis_markdown=synthesis_markdown,
            hypothesis_markdown=hypothesis_markdown,
            generation_mode="llm",
            comparisons=comparisons,
        )

    novelty_checks = build_novelty_checks(
        list(llm_ideas),
        pack,
        backend=request.novelty_backend,
    )
    experiment_contract = (
        build_experiment_contract(list(llm_ideas), pack)
        if request.include_experiment_contract
        else None
    )
    return replace(
        result,
        ideas=llm_ideas,
        novelty_checks=tuple(novelty_checks),
        experiment_contract=experiment_contract,
        synthesis_markdown=synthesis_markdown,
        hypothesis_markdown=hypothesis_markdown,
        generation_mode="llm",
        comparisons=comparisons,
        diagnostics=tuple(_diagnostics(pack, list(llm_ideas))),
    )


def _parse_evidence_comparisons(value: Any, pack: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Preserve explicit comparison work inside the existing synthesis result."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise LLMError("comparisons must be a list")
    available = allowed_evidence_refs(pack)
    rows = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise LLMError("comparison must be an object")
        dimension = _required_text(row, "dimension")
        interpretation = _required_text(row, "interpretation")
        relation = row.get("relation")
        if not isinstance(relation, str) or relation not in COMPARISON_RELATIONS:
            raise LLMError(f"comparisons[{index}].relation must be ONE string from {sorted(COMPARISON_RELATIONS)}; received {relation!r}")
        refs = _idea_string_list(row.get("evidence_refs"), "evidence_refs", index + 1, owner="comparisons")
        if not refs or set(refs) - available:
            raise LLMError(f"comparisons[{index}].evidence_refs requires known source evidence refs; unknown IDs: {sorted(set(refs) - available)}")
        normalized = {"dimension": dimension, "relation": relation, "evidence_refs": refs,
                      "interpretation": interpretation}
        for key in ("observations", "conditions"):
            normalized[key] = _idea_string_list(row.get(key, []), key, index + 1, allow_scalar=True, owner="comparisons")
        question = row.get("unresolved_question", "")
        if not isinstance(question, str):
            raise LLMError("comparison unresolved_question must be text")
        normalized["unresolved_question"] = question
        rows.append(normalized)
    return tuple(rows)


_IDEA_LIST_FIELDS = (
    "motivation_refs",
    "required_baselines",
    "required_datasets",
    "metrics",
    "risks",
)


def _parse_llm_idea_candidates(
    response: Mapping[str, Any],
    pack: Mapping[str, Any],
    *,
    limit: int,
) -> tuple[IdeaCandidate, ...] | None:
    """Validate an optional model candidate list without accepting new refs."""

    raw = response.get("idea_candidates")
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise LLMError(
            "LLM idea_candidates must be a non-empty JSON list when supplied."
        )

    allowed_refs = allowed_evidence_refs(pack)
    ideas: list[IdeaCandidate] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw[:limit], start=1):
        if not isinstance(item, Mapping):
            raise LLMError(f"LLM idea_candidates[{index - 1}] must be a JSON object.")
        for key in _IDEA_LIST_FIELDS:
            value = item.get(key)
            if value is not None and not isinstance(value, (list, str)):
                raise LLMError(
                    f"LLM idea_candidates[{index - 1}].{key} must be a JSON list "
                    "or string."
                )
            if key == "motivation_refs" and value is not None and not isinstance(value, list):
                raise LLMError(
                    f"LLM idea_candidates[{index - 1}].motivation_refs must be a JSON list."
                )
        idea_id = _required_idea_text(item, "idea_id", index)
        if idea_id in seen_ids:
            raise LLMError(
                f"LLM idea_candidates contains duplicate idea_id {idea_id!r}."
            )
        seen_ids.add(idea_id)
        for key in ("title", "hypothesis", "proposed_change"):
            _required_idea_text(item, key, index)
        refs = _idea_string_list(item.get("motivation_refs"), "motivation_refs", index)
        if not refs:
            raise LLMError(
                f"LLM idea_candidates[{index - 1}].motivation_refs must not be empty."
            )
        refs = [_resolve_evidence_ref(ref, allowed_refs) for ref in refs]
        unknown = sorted(set(refs) - allowed_refs)
        if unknown:
            raise LLMError(
                f"LLM idea_candidates[{index - 1}] contains unknown motivation refs: "
                + ", ".join(unknown)
            )
        normalized = dict(item)
        normalized["idea_id"] = idea_id
        normalized["motivation_refs"] = refs
        for key in _IDEA_LIST_FIELDS:
            if key in normalized:
                normalized[key] = _idea_string_list(
                    normalized[key],
                    key,
                    index,
                    allow_scalar=key != "motivation_refs",
                )
        ideas.append(IdeaCandidate.from_row(normalized))
    return tuple(ideas)


def _required_idea_text(
    row: Mapping[str, Any],
    key: str,
    index: int,
) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LLMError(
            f"LLM idea_candidates[{index - 1}].{key} must be a non-empty string."
        )
    return value.strip()


def _idea_string_list(
    value: object,
    key: str,
    index: int,
    *,
    allow_scalar: bool = False,
    owner: str = "idea_candidates",
) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        if allow_scalar and value.strip():
            return [value.strip()]
        raise LLMError(f"LLM {owner}[{index - 1}].{key} must be a JSON list.")
    if not isinstance(value, list):
        raise LLMError(
            f"LLM {owner}[{index - 1}].{key} must be a JSON list or string."
        )
    values: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise LLMError(
                f"LLM {owner}[{index - 1}].{key} must contain non-empty strings."
            )
        values.append(item.strip())
    return list(dict.fromkeys(values))


def allowed_evidence_refs(pack: Mapping[str, Any]) -> set[str]:
    """Collect identifiers the model may cite in a candidate motivation."""

    # Read's canonical handoff keeps the actual source-chunk IDs here, even
    # when a bounded card summarizes only a subset of those chunks. These
    # references are evidence, not new citations invented by synthesis.
    allowed: set[str] = set(_row_string_list(pack.get("evidence_refs")))
    for key in (
        "papers",
        "chunks",
        "paper_cards",
        "claim_cards",
        "method_cards",
        "dataset_cards",
        "code_links",
    ):
        rows = pack.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            for id_key in (
                "id",
                "document_id",
                "chunk_id",
                "paper_id",
                "claim_id",
                "method_id",
                "dataset_id",
                "link_id",
                "source_id",
            ):
                value = row.get(id_key)
                if isinstance(value, str) and value.strip():
                    allowed.add(value.strip())
            allowed.update(_row_string_list(row.get("evidence_refs")))
    return allowed


def _row_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _resolve_evidence_ref(reference: str, allowed_refs: set[str]) -> str:
    """Recover a uniquely shortened opaque evidence id without guessing.

    Long provider/document prefixes are easy for a model to drop while it is
    copying a chunk reference. A suffix match is safe only when it resolves
    to exactly one known id; ambiguous or unrelated values remain unchanged
    and are rejected by the existing evidence-boundary check.
    """

    if reference in allowed_refs:
        return reference
    matches = sorted(
        candidate for candidate in allowed_refs if candidate.endswith(reference)
    )
    return matches[0] if len(matches) == 1 else reference


def _has_evidence(pack: Mapping[str, Any]) -> bool:
    counts = pack.get("counts")
    if isinstance(counts, Mapping):
        return any(
            int(counts.get(key) or 0) > 0
            for key in ("documents", "chunks", "paper_cards", "claim_cards")
        )
    return any(pack.get(key) for key in ("papers", "paper_cards", "claim_cards"))


def _bounded_pack_json(pack: Mapping[str, Any]) -> str:
    """Serialize the shared evidence view used by model and readable notes."""

    return json.dumps(_selected_pack(pack), ensure_ascii=False, default=str)


def _selected_pack(pack: Mapping[str, Any]) -> dict[str, Any]:
    """Select current interpretations once, keeping source excerpts separate."""

    # Models cite original documents/passages, not generated card identities.
    # Card and note IDs belong to different namespaces and remain available in
    # persisted handoffs for existing consumers, but do not add evidence.
    source_refs = set(_row_string_list(pack.get("evidence_refs")))
    for key in ("paper_cards", "claim_cards", "method_cards", "dataset_cards", "paper_notes"):
        for row in _mapping_rows(pack.get(key)):
            source_refs.update(_row_string_list(row.get("evidence_refs")))
    for row in _mapping_rows(pack.get("papers")):
        reference = row.get("document_id") or row.get("id")
        if isinstance(reference, str) and reference.strip():
            source_refs.add(reference.strip())
    selected: dict[str, Any] = {
        "topic": pack.get("topic", ""),
        "coverage": pack.get("coverage", {}),
        "counts": pack.get("counts", {}),
        "limitations": pack.get("limitations", []),
        "interpretation_status": "Current adopted reading interpretations, not independent source verification; "
            "lookup passages remain source text and superseded notes remain only in the saved Read trace.",
        "question_assessments": pack.get("question_assessments", []),
        "question_assessment_status": "Chronological candidate-screening observations, not verified answers. "
            "A missing candidate is a search gap, not proof that no method exists; a direct candidate is "
            "not proof of its claims. Reconcile earlier gaps with later reading and source passages.",
        # Make the closed provenance boundary explicit in the model-facing
        # context.  The validator remains authoritative, but an exact
        # allowlist reduces avoidable retries when a model remembers a nearby
        # paper from the search pool instead of citing the selected evidence.
        "allowed_motivation_refs": sorted(source_refs & allowed_evidence_refs(pack)),
    }
    execution_context = _execution_context_text(pack)
    if execution_context:
        # User constraints are not optional background. Truncating the tail can
        # erase a permission or evaluation boundary while appearing complete.
        selected["execution_context"] = execution_context
        selected["execution_context_status"] = (
            "A requested goal is not evidence that code, data or an environment is ready. "
            "Preserve explicitly supplied execution constraints; otherwise identify what must be prepared, "
            "without describing missing assets as a prepared boundary."
        )
    selection: dict[str, Any] = {}
    for key in ("paper_cards", "claim_cards", "method_cards", "dataset_cards", "paper_notes"):
        value = pack.get(key)
        if isinstance(value, list):
            selected[key] = [
                {field: item for field, item in row.items()
                 if field not in {"claim_id", "method_id", "dataset_id"}}
                if isinstance(row, Mapping) else row for row in value[:24]
            ]
            selection[key] = {"included": min(len(value), 24), "available": len(value)}
    selected["paper_notes"] = [
        {**row,
         # These are local interpretation identities, not source evidence.
         # Exposing them beside deterministic claim IDs invites citations to
         # an invented hybrid identity. Keep the interpretation and its exact
         # source references; retain local identities in the saved Read trace.
         "claim_scopes": [
             {key: value for key, value in claim.items() if key != "claim_id"}
             for claim in _mapping_rows(row.get("claim_scopes"))
         ],
         "reading_followup": reading_followup_context(row.get("reading_followup"), include_passages=True)}
        for row in _mapping_rows(selected.get("paper_notes"))
    ]
    selected["context_selection"] = selection
    return selected


def _evidence_notes_markdown(pack: Mapping[str, Any]) -> str:
    """Render the same selected evidence for deterministic/human inspection."""

    pack = _selected_pack(pack)
    lines = [f"# Evidence Notes\n\nTopic: {pack.get('topic', '')}"]
    execution_context = _execution_context_text(pack)
    if execution_context:
        lines.extend(
            [
                "\n## User research request and execution context",
                pack["execution_context_status"],
                execution_context,
            ]
        )
    for key, heading in (
        ("paper_cards", "Papers"), ("claim_cards", "Claims"),
        ("method_cards", "Methods"), ("dataset_cards", "Datasets"),
        ("paper_notes", "Model Reading Notes"),
    ):
        rows = pack.get(key)
        if not isinstance(rows, list) or not rows:
            continue
        lines.append(f"\n## {heading}")
        if key == "paper_notes":
            lines.append(pack["interpretation_status"])
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            values = []
            for field_name, value in row.items():
                if value is None or value == "" or value == [] or value == {}:
                    continue
                text = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value).strip()
                # Keep the reader's compact notes intact, including caveats and
                # uncertainty; do not compress only the positive method story.
                if key != "paper_notes" and len(text) > 360:
                    text = text[:360] + " [truncated; consult source card]"
                values.append(f"{field_name}: {text}")
            if values:
                lines.append("- " + " | ".join(values))
        selection = pack["context_selection"][key]
        omitted = selection["available"] - selection["included"]
        if omitted:
            lines.append(f"[coverage] {omitted} {key} rows omitted; this is not the complete evidence set.")
    return "\n".join(lines)


def _execution_context_text(pack: Mapping[str, Any]) -> str:
    """Return the bounded downstream experiment context supplied by a caller."""

    value = pack.get("execution_context")
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return str(value or "").strip()


def _required_text(response: Mapping[str, Any], key: str) -> str:
    value = response.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LLMError(f"LLM synthesis response is missing non-empty {key}.")
    return value.strip()


def _generation_mode(value: object) -> Literal["deterministic", "llm"]:
    return "llm" if str(value or "").strip().lower() == "llm" else "deterministic"


def _diagnostics(pack: Mapping[str, Any], ideas: list[IdeaCandidate], *, require_ideas: bool = True) -> list[str]:
    """Report evidence gaps without blocking conservative synthesis."""
    diagnostics: list[str] = []
    pending = [str(row.get("paper_id") or "unknown") for row in _mapping_rows(pack.get("paper_notes"))
               if isinstance(row.get("reading_followup"), Mapping)
               and row["reading_followup"].get("pending_queries")]
    if pending:
        diagnostics.append("Unresolved source-reading questions remain: " + ", ".join(pending[:5]) + ".")
    # Facet gaps remain visible in ``gap_summary`` and the evidence pack.  They
    # are not, by themselves, a reason to block a bounded experiment: a small
    # user-provided baseline may intentionally test one method before the
    # literature search covers every planned facet.  Blocking is reserved for
    # an actually empty or untraceable evidence surface below.
    counts = pack.get("counts")
    if isinstance(counts, Mapping):
        if int(counts.get("documents") or 0) <= 0:
            diagnostics.append("No source documents are available.")
        if int(counts.get("chunks") or 0) <= 0:
            diagnostics.append("No source chunks are available.")
    if require_ideas and not any(idea.motivation_refs for idea in ideas):
        diagnostics.append("No idea candidate has an evidence reference.")
    return diagnostics


def _mapping_rows(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, Mapping)]


__all__ = [
    "SynthesisRequest",
    "SynthesisResult",
    "SynthesisStatus",
    "allowed_evidence_refs",
    "synthesize_evidence",
    "run_synthesis_capability",
]
