"""Read-only view separating declarations from executor observations.

An observed argv proves invocation, not that a method was implemented correctly.
Writing, revision and both review levels consume this same projection.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from simple_ar.report.schema import ReportContext


def execution_record(result: Mapping[str, Any]) -> dict[str, Any]:
    execution = result.get("execution")
    process = execution.get("process") if isinstance(execution, Mapping) else None
    if not isinstance(process, Mapping) or not process.get("invocation_id"):
        return {"observation_status": "not_recorded"}
    # Legacy top-level commands are not independent executor observations.
    record = {key: process[key] for key in (
        "invocation_id", "status", "argv", "cwd", "started_at", "finished_at",
        "timeout_sec", "duration_sec", "returncode", "stop_reason",
    ) if key in process}
    argv = record.get("argv")
    if isinstance(argv, list):
        record["argv"] = [str(value)[:1000] for value in argv[:64]]
        record["argv_truncated"] = len(argv) > 64 or any(len(str(value)) > 1000 for value in argv)
    record["observation_status"] = "executor_recorded"
    streams = process.get("streams")
    if isinstance(streams, Mapping):
        record["streams"] = {
            name: {key: value[key] for key in (
                "log_path", "bytes_seen", "log_bytes_discarded", "tail_bytes_discarded",
            ) if key in value}
            for name, value in streams.items() if isinstance(value, Mapping)
        }
    return record


def compact_failure_diagnosis(diagnosis: Mapping[str, Any]) -> dict[str, Any]:
    """A bounded report view of an observed failure, not a scientific verdict."""
    details = diagnosis.get("context")
    return {
        "status": str(diagnosis.get("status") or "unknown"),
        "summary": str(diagnosis.get("summary") or "")[:600],
        "deficiencies": [{key: row[key] for key in ("category", "code", "message") if key in row}
            for row in diagnosis.get("deficiencies", [])[:8] if isinstance(row, Mapping)]
            if isinstance(diagnosis.get("deficiencies"), list) else [],
        "stderr_tail": str(details.get("stderr_tail") or "")[-2200:] if isinstance(details, Mapping) else "",
    }


def report_execution_evidence(context: ReportContext) -> dict[str, Any]:
    """Preserve factual roles without loading files or rerunning an experiment."""
    plan = context.experiment_plan
    declared = {key: plan[key] for key in (
        "contract_id", "hypothesis", "dataset", "dataset_refs", "split_spec",
        "comparison_conditions", "metric_specs", "metrics", "expected_outcome",
        "resource_budget", "risks",
    ) if key in plan}
    results = context.results
    candidates: list[tuple[str, Mapping[str, Any]]] = [("current", results)]
    candidates.extend((role, results[role]) for role in ("baseline", "patched", "candidate")
                      if isinstance(results.get(role), Mapping))
    history = results.get("measurement_history")
    if isinstance(history, list):
        candidates.extend((str(row.get("action") or "history"), row)
                          for row in history if isinstance(row, Mapping))
    records, seen = [], set()
    for role, row in candidates:
        record = row.get("execution_record")
        record = dict(record) if isinstance(record, Mapping) else execution_record(row)
        identity = record.get("invocation_id")
        if not identity or identity in seen:
            continue
        seen.add(identity)
        records.append({"role": role, "artifact": str(row.get("artifact") or ""), **record})
    total = len(records)
    records = records if total <= 24 else [*records[:8], *records[-16:]]
    outputs, seen_outputs = [], set()
    for role, row in candidates:
        for output in row.get("output_evidence", []):
            if not isinstance(output, Mapping):
                continue
            identity = (output.get("measurement_artifact"), output.get("name"))
            if identity in seen_outputs:
                continue
            seen_outputs.add(identity)
            outputs.append({"role": role, **dict(output)})
    implementation = results.get("implementation")
    verification = implementation.get("method_validation") if isinstance(implementation, Mapping) else None
    return {
        "declared_protocol": declared,
        "declared_execution_context": {
            "text": context.execution_context[:5000],
            "total_characters": len(context.execution_context),
            "truncated": len(context.execution_context) > 5000,
        },
        "execution_records": records,
        "execution_records_total": total,
        "execution_records_omitted": total - len(records),
        "output_evidence": outputs[:24],
        "output_evidence_omitted": max(0, len(outputs) - 24),
        "implementation_verification": dict(verification) if isinstance(verification, Mapping) else {
            "status": "not_checked", "reason": "No independent method verification was supplied.",
        },
        "interpretation_boundaries": [
            "Declared protocol/context describes requested or configured conditions, not independently verified behavior.",
            "Executor records prove invocation and recorded completion, not dataset contents, algorithm correctness or unrecorded hardware.",
            "A timeout is a configured limit; duration_sec is the observed elapsed time.",
            "A passed run and correct metric values do not independently validate the implementation mechanism.",
            "Output previews are bounded producer text, not complete data or independent verification. An object_fields inventory shows only which top-level fields exist, not their values. Use a registered output handle/literal field query to read needed fields before reporting them missing; an absent attachment does not prove no file was produced.",
            "Producer attachments support recorded local observations, not literature attribution. Name their registered role in prose; do not require or fabricate paper citation keys or treat tool handles as Markdown link targets.",
        ],
    }
