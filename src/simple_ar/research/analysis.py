"""Standalone result-analysis boundary.

The existing ``result_analysis`` package remains the implementation backend.
This module only gives callers a small request boundary and keeps persistence
opt-in, so execution and analysis can be composed without coupling either one
to the eight-stage pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import json
from pathlib import Path
from typing import Any, Mapping

from simple_ar.core.capabilities import ArtifactRef, CapabilityContext, CapabilityResult
from simple_ar.experiment.execution.measurement import comparison_compatibility
from simple_ar.result_analysis.service import sample_std
from simple_ar.result_analysis.metrics import normalize_direction
from simple_ar.result_analysis.schema import AnalysisContext, AnalysisResult
from simple_ar.result_analysis.service import run_result_analysis


@dataclass(frozen=True, slots=True)
class AnalysisRequest:
    """Inputs for one deterministic or LLM-assisted result analysis."""

    context: AnalysisContext | Mapping[str, Any]
    use_llm: bool = False
    output_dir: Path | None = None
    label: str = "result-analysis"

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ValueError("AnalysisRequest.label cannot be empty.")


@dataclass(frozen=True, slots=True)
class AnalysisHandoff:
    """Typed representation of one persisted execution-to-analysis handoff."""

    execution_ref: ArtifactRef
    execution_status: str
    analysis: AnalysisResult

    def __post_init__(self) -> None:
        if not self.execution_status.strip():
            raise ValueError("AnalysisHandoff.execution_status cannot be empty.")

    def to_handoff_dict(self) -> dict[str, Any]:
        """Return the stable ``analysis_handoff.v1`` representation."""

        return {
            "schema_version": "analysis_handoff.v1",
            "execution_ref": self.execution_ref.to_dict(),
            "execution_status": self.execution_status,
            "analysis": self.analysis.model_dump(mode="json"),
        }

    @classmethod
    def from_handoff_dict(cls, data: Mapping[str, Any]) -> "AnalysisHandoff":
        """Restore an analysis handoff without reading or rerunning execution."""

        if str(data.get("schema_version") or "") != "analysis_handoff.v1":
            raise ValueError("Expected an analysis_handoff.v1 object.")
        execution_payload = data.get("execution_ref")
        analysis_payload = data.get("analysis")
        if not isinstance(execution_payload, Mapping):
            raise ValueError("Analysis handoff is missing execution_ref.")
        if not isinstance(analysis_payload, Mapping):
            raise ValueError("Analysis handoff is missing analysis.")
        execution_status = str(data.get("execution_status") or "").strip()
        if not execution_status:
            raise ValueError("Analysis handoff is missing execution_status.")
        return cls(
            execution_ref=ArtifactRef.from_dict(dict(execution_payload)),
            execution_status=execution_status,
            analysis=AnalysisResult.model_validate(analysis_payload),
        )


def analyze_results(
    request: AnalysisRequest,
    *,
    client: Any | None = None,
) -> AnalysisResult:
    """Analyze one result context and optionally persist its artifacts.

    No files are written unless ``request.output_dir`` is explicitly supplied.
    The existing result-analysis service owns metric normalization, claim
    grounding, audit behavior, and optional LLM use.
    """

    return run_result_analysis(
        request.context,
        output_dir=request.output_dir,
        client=client,
        use_llm=request.use_llm,
        label=request.label,
    )


def compare_experiment_results(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    primary_metric: str = "",
    metric_directions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Compare two canonical experiment results without running a policy.

    The comparison is deliberately small and conservative.  It consumes the
    top-level ``status`` and ``metrics`` fields emitted by the experiment
    boundary, uses only explicit metric directions, and returns
    ``inconclusive`` when the required evidence is absent.  It does not pick a
    winner, retry a run, or decide whether the research session should move.
    """

    baseline_data = _mapping_value(baseline)
    candidate_data = _mapping_value(candidate)
    primary = str(primary_metric or "").strip()
    directions, direction_sources = _comparison_directions(
        baseline_data,
        candidate_data,
        metric_directions,
    )
    if not primary:
        primary = _embedded_primary_metric(candidate_data) or _embedded_primary_metric(
            baseline_data
        )

    baseline_status = _result_status(baseline_data)
    candidate_status = _result_status(candidate_data)
    baseline_metrics = _numeric_metrics(baseline_data.get("metrics"))
    candidate_metrics = _numeric_metrics(candidate_data.get("metrics"))
    metric_rows = _comparison_metric_rows(
        baseline_metrics,
        candidate_metrics,
        directions,
        direction_sources,
        primary,
    )
    verdict, reasons = _comparison_verdict(
        baseline_data=baseline_data,
        candidate_data=candidate_data,
        baseline_status=baseline_status,
        candidate_status=candidate_status,
        metric_rows=metric_rows,
        primary_metric=primary,
    )
    compatibility, compatibility_reason = comparison_compatibility(baseline_data, candidate_data)
    not_comparable = compatibility in {"unknown", "mismatched"}
    if compatibility == "declared_match" and (baseline_status != "passed" or candidate_status != "passed"):
        not_comparable = True
        compatibility_reason = "Declared protocols match, but a failed execution cannot establish scientific improvement."
    if not_comparable:
        verdict = "inconclusive"
        reasons = [compatibility_reason]
        for row in metric_rows:
            row["interpretation"] = "not_comparable"
    else:
        reasons.append(compatibility_reason)
    return {
        "schema_version": "experiment_comparison.v1",
        "status": "ready" if baseline_data and candidate_data else "incomplete",
        "verdict": verdict,
        "comparability": compatibility,
        "reasons": reasons,
        "metric_config": {
            "primary_metric": primary,
            "metric_directions": directions,
        },
        "baseline": _comparison_result_summary(baseline_data, baseline_status, baseline_metrics),
        "candidate": _comparison_result_summary(candidate_data, candidate_status, candidate_metrics),
        "metrics": metric_rows,
        "deltas": {row["name"]: row["delta"] for row in metric_rows},
    }


def _comparison_directions(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
    explicit: Mapping[str, str] | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Collect embedded directions, with caller configuration taking priority."""

    directions: dict[str, str] = {}
    sources: dict[str, str] = {}
    for result in (baseline, candidate):
        schema = result.get("result_schema")
        if not isinstance(schema, Mapping):
            continue
        embedded = schema.get("metric_directions")
        if isinstance(embedded, Mapping):
            for name, value in embedded.items():
                _set_direction(directions, sources, name, value, "embedded")
        schema_primary = str(schema.get("primary_metric") or "").strip()
        schema_direction = schema.get("direction")
        if schema_primary and schema_direction is not None:
            _set_direction(
                directions,
                sources,
                schema_primary,
                schema_direction,
                "embedded",
            )
    for name, value in (explicit or {}).items():
        _set_direction(directions, sources, name, value, "configured")
    return directions, sources


def _set_direction(
    directions: dict[str, str],
    sources: dict[str, str],
    name: object,
    value: object,
    source: str,
) -> None:
    metric_name = str(name).strip()
    if not metric_name:
        return
    directions[metric_name] = normalize_direction(value)
    sources[metric_name] = source


def _comparison_metric_rows(
    baseline: Mapping[str, float],
    candidate: Mapping[str, float],
    directions: Mapping[str, str],
    direction_sources: Mapping[str, str],
    primary_metric: str,
) -> list[dict[str, Any]]:
    baseline_names = {_metric_key(name): (str(name), value) for name, value in baseline.items()}
    candidate_names = {_metric_key(name): (str(name), value) for name, value in candidate.items()}
    rows: list[dict[str, Any]] = []
    for key in sorted(set(baseline_names) & set(candidate_names)):
        baseline_name, baseline_value = baseline_names[key]
        _, candidate_value = candidate_names[key]
        name = baseline_name
        direction, source = _direction_for_metric(name, directions, direction_sources)
        delta = candidate_value - baseline_value
        if abs(delta) <= 1e-12:
            interpretation = "unchanged"
        elif direction == "higher":
            interpretation = "improved" if delta > 0 else "regressed"
        elif direction == "lower":
            interpretation = "improved" if delta < 0 else "regressed"
        elif direction == "resource":
            interpretation = "decreased" if delta < 0 else "increased"
        elif direction == "ignore":
            interpretation = "ignored"
        else:
            interpretation = "changed"
        rows.append(
            {
                "name": name,
                "baseline": baseline_value,
                "candidate": candidate_value,
                "delta": delta,
                "direction": direction,
                "direction_source": source,
                "interpretation": interpretation,
                "is_primary": bool(primary_metric and _same_metric(name, primary_metric)),
            }
        )
    return rows


def _comparison_verdict(
    *,
    baseline_data: Mapping[str, Any],
    candidate_data: Mapping[str, Any],
    baseline_status: str,
    candidate_status: str,
    metric_rows: list[dict[str, Any]],
    primary_metric: str,
) -> tuple[str, list[str]]:
    if not baseline_data or not candidate_data:
        return "inconclusive", ["baseline or candidate result is missing"]
    if baseline_status != "passed" and candidate_status == "passed":
        return "improved", [f"candidate passed after baseline status `{baseline_status}`"]
    if baseline_status == "passed" and candidate_status != "passed":
        return "regressed", [f"candidate status `{candidate_status}` after passing baseline"]
    if candidate_status != "passed":
        return "inconclusive", [
            "both results are non-passing or blocked: "
            f"baseline={baseline_status}, candidate={candidate_status}"
        ]

    directional = [
        row
        for row in metric_rows
        if row.get("direction") in {"higher", "lower"}
    ]
    if primary_metric:
        primary_rows = [
            row for row in metric_rows if _same_metric(str(row.get("name", "")), primary_metric)
        ]
        if not primary_rows:
            return "inconclusive", [
                f"configured primary metric `{primary_metric}` was not shared by both results"
            ]
        if primary_rows[0].get("direction") not in {"higher", "lower"}:
            return "inconclusive", [
                f"primary metric `{primary_metric}` has no directional comparison"
            ]
        directional = [row for row in directional if row in primary_rows]
    if not directional:
        return "inconclusive", ["no directional numeric metrics were shared by both results"]

    improved = [row for row in directional if row["interpretation"] == "improved"]
    regressed = [row for row in directional if row["interpretation"] == "regressed"]
    if improved and not regressed:
        return "improved", _comparison_reasons(improved, "improved")
    if regressed and not improved:
        return "regressed", _comparison_reasons(regressed, "regressed")
    if improved and regressed:
        return "mixed", _comparison_reasons(improved, "improved") + _comparison_reasons(
            regressed, "regressed"
        )
    return "unchanged", ["directional metrics were unchanged within tolerance"]


def _comparison_reasons(rows: list[dict[str, Any]], prefix: str) -> list[str]:
    return [
        f"{prefix} `{row['name']}` by {float(row['delta']):+.6g}"
        for row in rows[:6]
    ]


def _comparison_result_summary(
    result: Mapping[str, Any],
    status: str,
    metrics: Mapping[str, float],
) -> dict[str, Any]:
    execution = result.get("execution")
    execution = execution if isinstance(execution, Mapping) else {}
    return {
        "status": status,
        "returncode": result.get("returncode"),
        "timed_out": result.get("timed_out"),
        "duration_sec": execution.get("duration_sec"),
        "metrics": dict(metrics),
    }


def _result_status(result: Mapping[str, Any]) -> str:
    # A zero exit code cannot override an independently rejected measurement.
    # Interpret the retained evidence; do not rewrite its canonical original.
    guard = _mapping_value(result.get('guard'))
    integrity = _mapping_value(_mapping_value(result.get('measurement')).get('asset_integrity'))
    if guard.get('status') == 'failed' or integrity.get('status') == 'changed':
        return 'failed'
    status = str(result.get("status") or "").strip()
    if status:
        return status
    execution = result.get("execution")
    if isinstance(execution, Mapping):
        return str(execution.get("status") or "missing").strip() or "missing"
    return "missing"


def _numeric_metrics(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    metrics: dict[str, float] = {}
    for name, raw in value.items():
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        numeric = float(raw)
        if math.isfinite(numeric):
            metrics[str(name).strip()] = numeric
    return {name: value for name, value in metrics.items() if name}


def _embedded_primary_metric(result: Mapping[str, Any]) -> str:
    primary = str(result.get("primary_metric") or "").strip()
    if primary:
        return primary
    schema = result.get("result_schema")
    if isinstance(schema, Mapping):
        return str(schema.get("primary_metric") or "").strip()
    return ""


def _direction_for_metric(
    name: str,
    directions: Mapping[str, str],
    sources: Mapping[str, str],
) -> tuple[str, str]:
    if name in directions:
        return str(directions[name]), str(sources.get(name) or "configured")
    key = _metric_key(name)
    for configured_name, direction in directions.items():
        if _metric_key(configured_name) == key:
            return str(direction), str(sources.get(configured_name) or "configured")
    return "unknown", "none"


def _metric_key(name: object) -> str:
    return str(name).strip().lower()


def _same_metric(left: str, right: str) -> bool:
    return _metric_key(left) == _metric_key(right)


def _mapping_value(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _merge_result_schema_into_analysis_context(
    context: AnalysisContext,
    result_schema: Mapping[str, Any],
) -> AnalysisContext:
    """Expose execution-required metrics without mutating the context."""

    schema = dict(result_schema)
    expected = [
        dict(item) for item in context.expected_metrics if isinstance(item, Mapping)
    ]
    known = {
        str(item.get("name")).strip()
        for item in expected
        if str(item.get("name") or "").strip()
    }
    names: list[str] = []
    primary = str(schema.get("primary_metric") or "").strip()
    if primary:
        names.append(primary)
    required = schema.get("required_metrics")
    if isinstance(required, (list, tuple)):
        names.extend(str(name).strip() for name in required if str(name).strip())
    direction = str(schema.get("direction") or "").strip()
    schema_directions = schema.get("metric_directions")
    schema_directions = (
        schema_directions if isinstance(schema_directions, Mapping) else {}
    )
    for name in dict.fromkeys(names):
        if name in known:
            continue
        row: dict[str, Any] = {"name": name}
        metric_direction = str(schema_directions.get(name) or direction).strip()
        if metric_direction:
            row["direction"] = metric_direction
        expected.append(row)
    if expected == context.expected_metrics:
        return context
    return context.model_copy(update={"expected_metrics": expected})


def _measurement_group(result: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    from simple_ar.experiment.execution.measurement import comparable_protocol
    measurement = result['measurement']
    protocol = comparable_protocol(result['experiment_contract'], observed_assets=measurement.get('asset_integrity'))
    conditions = dict(protocol['comparison_conditions'] or {})
    conditions.pop('seed', None)
    protocol['comparison_conditions'] = conditions
    key = json.dumps([protocol, result.get('result_schema', {}),
        measurement.get('asset_integrity', {}).get('content_fingerprint')], sort_keys=True)
    return key, protocol


def _experiment_set_payload(context: CapabilityContext, collection: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve the declared collection; reuse the single-pair comparison rules."""
    comparisons, rows, missing, failed = [], [], [], []
    groups, candidate_groups = {}, {}
    policy = collection.get('baseline_policy', 'run')
    if policy not in {'run', 'skip'} or policy == 'skip' and any(pair['baseline'] is not None for pair in collection['pairs']):
        raise ValueError('A skipped baseline cannot hide a supplied measurement')
    statuses = []
    candidate_implementations: list[Mapping[str, Any] | None] = []
    for pair in collection["pairs"]:
        measured = {}
        for role in ("baseline", "candidate"):
            reference = pair[role]
            if reference is None:
                if role != 'baseline' or policy != 'skip':
                    missing.append({"seed": pair["seed"], "condition": role})
                continue
            ref = ArtifactRef.from_dict(reference)
            result = context.read_input_json(ref)
            measured[role] = result
            status = _result_status(result)
            statuses.append(status)
            if role == "candidate":
                implementation_ref = result.get("implementation_ref")
                candidate_implementations.append(
                    dict(implementation_ref) if isinstance(implementation_ref, Mapping) else None
                )
            # Failed runs remain in the set/comparison, never in valid metric rows.
            if status == "passed":
                rows.append({"condition": role, "seed": pair["seed"], "metrics": result["metrics"],
                             "artifact": ref.path})
                if policy == 'skip':
                    measurement = result.get('measurement') or {}
                    contract = result.get('experiment_contract') or {}
                    if (measurement.get('source_kind') == 'measured' and measurement.get('protocol_fingerprint')
                            and all(contract.get(key) for key in ('dataset_refs', 'split_spec', 'metric_specs', 'comparison_conditions'))
                            and contract['split_spec'].get('status') != 'not_declared_by_framework'):
                        key, protocol = _measurement_group(result)
                    else:
                        key, protocol = ref.path, {}  # No inferred pooling of undeclared conditions.
                    group = candidate_groups.setdefault(key, {'protocol': protocol, 'rows': []})
                    group['rows'].append(rows[-1])
            else:
                failed.append({"seed": pair["seed"], "condition": role, "status": status, "artifact": ref.path})
        if len(measured) == 2:
            comparison = compare_experiment_results(measured["baseline"], measured["candidate"])
            comparisons.append({**comparison, "seed": pair["seed"],
                                "baseline_ref": pair["baseline"], "candidate_ref": pair["candidate"]})
            if comparison["comparability"] == "declared_match" and all(_result_status(r) == "passed" for r in measured.values()):
                baseline = measured["baseline"]
                key, protocol = _measurement_group(baseline)
                group = groups.setdefault(key, {"protocol": protocol, "pairs": []})
                group["pairs"].append(comparisons[-1])
    summaries = []
    for group_id, group in enumerate(groups.values()):
        names = {row["name"] for pair in group["pairs"] for row in pair["metrics"]}
        for name in sorted(names):
            selected = [(pair, row) for pair in group["pairs"] for row in pair["metrics"] if row["name"] == name]
            values = [row["delta"] for _, row in selected]
            summaries.append({"group_id": group_id, "metric": name, "n": len(values),
                "unit": next((spec.get("unit", "") for spec in group["protocol"].get("metric_specs", []) if spec["name"] == name), ""),
                "seeds": [pair["seed"] for pair, _ in selected],
                "baseline_mean": sum(row["baseline"] for _, row in selected) / len(values),
                "candidate_mean": sum(row["candidate"] for _, row in selected) / len(values),
                "delta_mean": sum(values) / len(values), "delta_sample_std": sample_std(values),
                "protocol": group["protocol"], "sources": [{"baseline_ref": pair["baseline_ref"],
                    "candidate_ref": pair["candidate_ref"]} for pair, _ in selected]})
    condition_summaries = []
    for group_id, group in enumerate(candidate_groups.values()):
        for name in sorted({name for row in group['rows'] for name in row['metrics']}):
            selected = [row for row in group['rows'] if type(row['metrics'].get(name)) in (int, float)
                and math.isfinite(row['metrics'][name])]
            values = [row['metrics'][name] for row in selected]
            if values:
                condition_summaries.append({'group_id': group_id, 'metric': name, 'n': len(values),
                    'candidate_mean': sum(values) / len(values), 'candidate_sample_std': sample_std(values),
                    'seeds': [row['seed'] for row in selected], 'sources': [row['artifact'] for row in selected],
                    'protocol': group['protocol']})
    metrics = _unique_candidate_summary_metrics(condition_summaries if policy == 'skip' else summaries)
    recorded_implementations = [ref for ref in candidate_implementations if ref is not None]
    if recorded_implementations and any(ref != recorded_implementations[0] for ref in recorded_implementations):
        raise ValueError("The paired candidate measurements contain different implementation revisions.")
    collection_implementation = collection.get("implementation_ref")
    if (isinstance(collection_implementation, Mapping) and recorded_implementations
            and dict(collection_implementation) != recorded_implementations[0]):
        raise ValueError("The result collection disagrees with its candidate measurement lineage.")
    complete_lineage = (
        bool(candidate_implementations)
        and len(candidate_implementations) == len(collection["pairs"])
        and all(ref is not None for ref in candidate_implementations)
    )
    limitations = [
        "Paired summaries are descriptive, grouped by declared protocol and observed protected-file identity. "
        "Only the current candidate revision is included; no significance test or population uncertainty is established."
    ]
    if policy == 'skip':
        limitations = ['Candidate-only descriptive summaries; no baseline comparison, significance test or population uncertainty is established. Undeclared conditions are not pooled.']
    if collection_implementation is not None and not complete_lineage:
        limitations.append("Candidate implementation lineage could not be verified for every measured pair.")
    gaps = [f"seed={row['seed']}: missing {row['condition']}" for row in missing]
    gaps.extend(f"seed={row['seed']}: {row['condition']} status={row['status']}" for row in failed)
    gaps.extend(f"seed={row['seed']}: comparison {row['comparability']}" for row in comparisons
                if row['comparability'] != 'declared_match')
    if policy == 'run' and not summaries:
        gaps.append('No eligible paired metric summary; no compatible conditions were established.')
    if policy == 'skip' and not condition_summaries:
        gaps.append('No valid candidate metrics were available for descriptive analysis.')
    limitations.extend(gaps)
    return {"status": "passed" if statuses and not gaps and all(s == "passed" for s in statuses) else "incomplete",
            "metrics": metrics, "comparisons": comparisons, "seed_evidence": rows,
            "paired_summary": summaries,
            **({'condition_summary': condition_summaries, 'baseline_policy': 'skip'} if policy == 'skip' else {}),
            "implementation_ref": recorded_implementations[0] if complete_lineage else None,
            "candidate_revision": collection.get("candidate_revision", 0),
            "superseded_candidates": collection.get("superseded_candidates", []),
            "missing_measurements": missing, "failed_measurements": failed, "planned_pairs": len(collection["pairs"]),
            "coverage_gaps": gaps,
            "limitations": limitations}


def _unique_candidate_summary_metrics(
    summaries: list[Mapping[str, Any]],
) -> dict[str, float]:
    """Expose only unambiguous candidate aggregates as analysis metrics."""
    values_by_name: dict[str, list[float]] = {}
    for summary in summaries:
        name = str(summary.get("metric") or "").strip()
        value = summary.get("candidate_mean")
        if (not name or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))):
            continue
        values_by_name.setdefault(name, []).append(float(value))
    return {
        name: values[0]
        for name, values in values_by_name.items()
        if values and all(math.isclose(value, values[0], rel_tol=1e-12,
                                       abs_tol=1e-12) for value in values)
    }


def analyze_experiment_capability(
    *,
    context: CapabilityContext,
    result_ref: ArtifactRef,
    analysis_context: AnalysisContext | Mapping[str, Any],
    client: Any | None = None,
    use_llm: bool = False,
    label: str = "experiment-analysis",
    baseline_ref: ArtifactRef | None = None,
) -> CapabilityResult:
    """Analyze a declared execution result through the session boundary.

    The execution artifact is read from an explicit input reference. The
    adapter writes one analysis handoff and keeps the execution reference
    instead of copying the canonical result into another file. It analyzes
    failed executions as well, so a failed run remains evidence rather than
    disappearing from the research session.
    """
    payload = context.read_input_json(result_ref)
    if not isinstance(payload, Mapping):
        raise ValueError("Experiment result artifact must be a JSON object.")
    is_collection = result_ref.kind == "experiment_set"
    if is_collection:
        if baseline_ref is not None:
            raise ValueError("A result set already declares its paired baselines.")
        payload = _experiment_set_payload(context, payload)
    comparison_ref = None
    collection_analysis_ref = None
    if is_collection:
        collection_analysis_ref = context.store.write_json(
            "paired_analysis.json", {"schema_version": "experiment_set_analysis.v1",
                "collection_ref": result_ref.to_dict(), **payload},
            kind="experiment_set_analysis", schema="experiment_set_analysis.v1", producer="research.analysis",
        )
    if baseline_ref is not None:
        baseline = context.read_input_json(baseline_ref)
        if not isinstance(baseline, Mapping):
            raise ValueError("Baseline result artifact must be a JSON object.")
        comparison = compare_experiment_results(baseline, payload)
        comparison["baseline_ref"] = baseline_ref.to_dict()
        comparison["candidate_ref"] = result_ref.to_dict()
        comparison_ref = context.store.write_json(
            "comparison.json", comparison, kind="experiment_comparison",
            schema="experiment_comparison.v1", producer="research.analysis",
        )
        payload = {**payload, "comparisons": [*payload.get("comparisons", []), comparison]}
    execution_status = _result_status(payload)
    result_schema = payload.get("result_schema")
    result_schema = result_schema if isinstance(result_schema, Mapping) else {}
    base_context = (
        analysis_context
        if isinstance(analysis_context, AnalysisContext)
        else AnalysisContext.model_validate(analysis_context)
    )
    base_context = _merge_result_schema_into_analysis_context(base_context, result_schema)
    metrics = dict(base_context.metrics)
    observed_metrics = payload.get("metrics")
    if isinstance(observed_metrics, Mapping):
        metrics.update(
            {
                str(name): float(value)
                for name, value in observed_metrics.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            }
        )
    project_results = dict(base_context.project_results)
    project_results.pop("implementation_ref", None)  # Legacy global-active hint is not measurement lineage.
    project_results["execution_result"] = dict(payload)
    diagnosis_refs = [ref for ref in context.inputs if ref.kind == "experiment_diagnosis"]
    if diagnosis_refs and execution_status != "passed":
        diagnosis = context.read_input_json(diagnosis_refs[-1])
        if isinstance(diagnosis, Mapping):
            detail = diagnosis.get("context")
            project_results["failure_diagnosis"] = {
                "artifact": diagnosis_refs[-1].path,
                "summary": str(diagnosis.get("summary") or "")[:600],
                "deficiencies": [
                    {key: row[key] for key in ("category", "code", "message") if key in row}
                    for row in diagnosis.get("deficiencies", [])[:8] if isinstance(row, Mapping)
                ] if isinstance(diagnosis.get("deficiencies"), list) else [],
                "stderr_tail": str(detail.get("stderr_tail") or "")[-2200:]
                if isinstance(detail, Mapping) else "",
            }
    # A measurement must carry the implementation revision that produced it.
    # Do not fall back to the application's globally active implementation:
    # that would let a later candidate's method evidence explain an older run.
    implementation_ref = payload.get("implementation_ref")
    method_validation = None
    if isinstance(implementation_ref, Mapping):
        implementation = ArtifactRef.from_dict(dict(implementation_ref))
        if implementation not in context.inputs:
            raise ValueError("The measured implementation revision is not a declared analysis input.")
        implementation_record = context.read_input_json(implementation)
        if not isinstance(implementation_record, Mapping):
            raise ValueError("Implementation result artifact must be a JSON object.")
        recorded_check = implementation_record.get("method_validation")
        method_validation = dict(recorded_check) if isinstance(recorded_check, Mapping) else {
            "status": "未检查",
            "reason": "This implementation predates candidate-specific method checks.",
        }
        project_results["implementation"] = {
            "artifact_ref": implementation.to_dict(),
            "status": implementation_record.get("status"),
            "method_validation": method_validation,
        }
    analysis_metadata = dict(base_context.metadata)
    if method_validation is not None:
        analysis_metadata["method_validation"] = method_validation
    if is_collection:
        project_results["seed_evidence"] = payload["seed_evidence"]
        project_results["comparisons"] = payload["comparisons"]
        project_results["paired_summary"] = payload["paired_summary"]
        if 'condition_summary' in payload:
            project_results['condition_summary'] = payload['condition_summary']
    analysis = analyze_results(
        AnalysisRequest(
            context=base_context.model_copy(
                update={"metrics": metrics, "project_results": project_results,
                        "metadata": analysis_metadata}
            ),
            use_llm=use_llm,
            label=label,
        ),
        client=client,
    )
    if method_validation is not None:
        status = str(method_validation.get("status") or "未检查")
        reason = str(method_validation.get("reason") or "No candidate-specific behavior check was recorded.")
        limitation = f"Candidate method validation is {status}: {reason}"
        analysis.audit.limitations = list(dict.fromkeys([*analysis.audit.limitations, limitation]))
    preparation = payload.get("preparation")
    if is_collection:
        limitations = payload["limitations"]
        if payload['coverage_gaps']:
            if analysis.status == 'passed':
                analysis.status = 'incomplete'
            analysis.status_reasons = list(dict.fromkeys([*analysis.status_reasons, *payload['coverage_gaps']]))
        analysis.audit.limitations = list(dict.fromkeys([*analysis.audit.limitations, *limitations]))
        coverage = ('Candidate measurement coverage', 'candidate runs') if payload.get('baseline_policy') == 'skip' else ('Paired measurement coverage', 'pairs')
        analysis.readme_markdown += f"\n\n## {coverage[0]}\n\n" + (
            f"Planned {coverage[1]}: {payload['planned_pairs']}; paired comparisons: {len(payload['comparisons'])}; "
            f"missing measurements: {len(payload['missing_measurements'])}; failed measurements: {len(payload['failed_measurements'])}.\n\n"
            + "\n".join(f"- {item}" for item in limitations) + "\n"
        )
    if preparation is not None:
        limitations = preparation["limitations"]
        analysis.audit.limitations = list(dict.fromkeys([*analysis.audit.limitations, *limitations]))
        if limitations:
            analysis.readme_markdown = analysis.readme_markdown.rstrip() + (
                "\n\n## Preparation limitations\n\n" + "\n".join(f"- {item}" for item in limitations) + "\n"
            )
    handoff = AnalysisHandoff(
        execution_ref=result_ref,
        execution_status=execution_status,
        analysis=analysis,
    )
    capability_status = _capability_status_for_analysis(analysis.status)
    output = context.store.write_json(
        "analysis.json",
        handoff.to_handoff_dict(),
        kind="analysis_result",
        schema="analysis_handoff.v1",
        producer="research.analysis",
    )
    diagnostics = (
        (f"Execution input status: {execution_status}.",)
        if execution_status != "passed"
        else ()
    )
    diagnostics = tuple(
        dict.fromkeys((*diagnostics, *analysis.status_reasons))
    )
    return CapabilityResult(
        status=capability_status,
        artifacts=(output,) + tuple(ref for ref in (comparison_ref, collection_analysis_ref) if ref is not None),
        diagnostics=diagnostics,
        usage={
            "execution_status": execution_status,
            "metric_count": len(metrics),
            "claim_count": len(analysis.claims),
        },
        provenance={
            "capability": "analysis",
            "execution_ref": result_ref.path,
            "result_schema": "analysis_handoff.v1",
            "mode": "llm" if use_llm else "deterministic",
            "model": str(getattr(client, "model", "")) if use_llm else "",
        },
    )


def _capability_status_for_analysis(status: str) -> str:
    """Map analysis semantics to the smaller capability status vocabulary."""

    return {
        "passed": "completed",
        "incomplete": "partial",
        "metric_below_target": "partial",
        "failed": "failed",
        "blocked": "blocked",
    }.get(status, "failed")


__all__ = [
    "AnalysisRequest",
    "AnalysisHandoff",
    "analyze_results",
    "compare_experiment_results",
    "analyze_experiment_capability",
]
