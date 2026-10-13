"""Minimal historical wire-format fixture; no planner, model or process runs.

Keep these dictionaries independent of current serializers: compatibility tests
must read the old format, not regenerate history with today's implementation.
New workflow execution belongs in test_research_application.py.
"""

import json
from pathlib import Path

from simple_ar.app.research_session import load_research_session_result


def historical_session(
    root: Path, *, failed: bool = False, code_task: bool = False,
    aggregate_brief: bool = False, with_design: bool = True,
):
    topic = "reliable agents"
    status = "failed" if failed else "passed"
    contract = {
        "contract_id": "fixture-contract", "hypothesis": "Validation improves reliability.",
        "baseline": "fixture baseline", "dataset": "fixture", "metrics": ["accuracy"],
        "proposed_change": "Add validation", "validation_hints": ["Report accuracy"],
    }
    paper = {"id": "paper-1", "title": "Reliable agents", "abstract": "Validation improves reliability."}
    payloads = [
        ("plan-001", "plan", "research_plan", "research_plan.v1", {
            "research_questions": {"questions": [{"question_id": "RQ1", "question": topic}]},
            "query_plan": {"topic": topic, "seed_queries": [topic], "queries": [topic]},
            "source_plan": {"queries": [topic], "sources": ["local"]},
        }),
        ("search-001", "search", "search_result", "search_handoff.v1", {
            "status": "completed", "papers": [paper], "selected_paper_ids": ["paper-1"], "responses": [],
        }),
        ("document-001", "document_ingest", "document_bundle", "document_bundle.v1", {
            "documents": [{"document_id": "paper-1", "source": "fixture", "title": paper["title"],
                           "abstract": paper["abstract"], "metadata": {"paper_id": "paper-1"}}],
            "sections": [], "chunks": [], "fulltext_manifest": {}, "fulltext_extraction": {},
        }),
        ("read-001", "read", "read_result", "read_result.v1", {
            "status": "completed", "paper_notes": [{"paper_id": "paper-1", "summary": paper["abstract"]}],
        }),
        ("synthesize-001", "synthesize", "synthesis_result", "synthesis_result.v1", {
            "status": "ready", "gap_summary": "Validate reliability.", "ideas": [], "novelty_checks": [],
            "experiment_contract": contract, "synthesis_markdown": "Validation improves reliability.",
        }),
        ("design-001", "research_design", "research_design", "research_design.v1", {
            "status": "ready", "contract": contract,
        }),
        ("experiment-001", "experiment", "experiment_result", "canonical_results.2.5", {
            "status": status, "metrics": {} if failed else {"accuracy": 0.75},
            "result_schema": {"primary_metric": "accuracy", "required_metrics": ["accuracy"],
                              "metric_directions": {"accuracy": "higher"}},
        }),
        ("analysis-001", "analysis", "analysis_result", "analysis_handoff.v1", {
            "execution_status": status,
            "execution_ref": {"path": "attempts/experiment-001/results.json", "kind": "experiment_result",
                              "schema": "canonical_results.2.5"},
            "analysis": {"status": "incomplete" if failed else "passed",
                         "status_reasons": ["Fixture process failed"] if failed else [],
                         "readme_markdown": "Recorded historical analysis; not a live experiment."},
        }),
    ]

    def write(relative, value):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2), encoding="utf-8")

    synthesis = next(row[4] for row in payloads if row[0] == "synthesize-001")
    if aggregate_brief:
        read = next(row[4] for row in payloads if row[0] == "read-001")
        payloads = [row for row in payloads if row[0] not in {"read-001", "synthesize-001"}]
        payloads.append(("brief-001", "research_brief", "research_brief", "research_brief.v1", {
            "status": "ready", "topic": topic,
            "read": {"schema_version": "read_result.v1", **read},
            "synthesis": {"schema_version": "synthesis_result.v1", **synthesis},
        }))
    if not with_design:
        payloads = [row for row in payloads if row[0] != "design-001"]
    source_ref = {"path": "inputs/code_task_input.json", "kind": "research_input",
                  "schema": "research_code_task_input.v1"}
    if code_task:
        write(source_ref["path"], {
            "schema_version": source_ref["schema"], "topic": topic,
            "synthesis": {"schema_version": "synthesis_result.v1", **synthesis},
        })
    for attempt, capability, kind, schema, body in payloads:
        filename = {"experiment": "results.json", "analysis": "analysis.json"}.get(capability, f"{kind}.json")
        relative = f"attempts/{attempt}/{filename}"
        write(relative, {"schema_version": schema, **body})
        write(f"attempts/{attempt}/attempt_manifest.json", {
            "schema_version": "attempt_manifest.v1", "attempt_id": attempt, "capability": capability,
            "status": "failed" if failed and capability == "experiment" else "completed",
            "parent_attempt": "experiment-001" if capability == "analysis" else None,
            "outputs": [{"path": filename, "kind": kind, "schema": schema}],
            **({"inputs": [source_ref] + ([{
                "path": "attempts/design-001/research_design.json", "kind": "research_design",
                "schema": "research_design.v1",
            }] if with_design else [])} if code_task and capability == "experiment" else {}),
        })
    write("session_manifest.json", {
        "schema_version": "session_manifest.v1", "session_id": "historical-fixture", "topic": topic,
        "profile": "full_research", "status": "running", "state_refs": {},
        "budget": {"max_attempts": 10, "max_no_progress": 3, "attempts": 8},
        "decisions": [{"capability": "analysis", "attempt_id": "analysis-001", "action": "accept",
                       "result_status": "completed", "next_capability": "experiment" if failed else "report"}],
    })
    if code_task:
        from simple_ar.app.research_code_task import load_research_code_task_session_result
        return load_research_code_task_session_result(root)
    return load_research_session_result(root)


def session_files(root: Path):
    """Snapshot all saved evidence, including manifests and failed records."""
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
