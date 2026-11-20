"""Check the compatibility API for stepwise research sessions locally.

This smoke uses a local literature fixture, a one-line experiment, and a
fixture report writer. It checks the public ResearchApplication lifecycle
and its saved state references.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import sys

from simple_ar.app.research_application import (
    ResearchApplicationServices,
    create_session,
)
from simple_ar.app.session_roots import new_research_session_root
from simple_ar.research.workflow_contracts import ResearchBrief


class FixtureReportWriter:
    """Return bounded section JSON for the offline report smoke only."""

    def __init__(self, paper_id: str):
        self.paper_id = paper_id

    def ask_json(self, _system: str, _prompt: str, *, label: str, **_kwargs):
        if label.startswith("report-writer"):
            return {
                "draft_markdown": f"The smoke experiment reports accuracy 0.75 [@{self.paper_id}].",
                "used_sources": [self.paper_id],
                "citations": [self.paper_id],
            }
        raise AssertionError(f"Unexpected fixture LLM call: {label}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("runs/research-session-smoke"),
        help="Directory under which the unique session directory is created.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    fixture = repo_root / "tests" / "fixtures" / "research" / "reliable_agents.md"
    topic = "reliable agents"
    execution = {
        "command": [sys.executable, "-c", "print('accuracy: 0.75')"],
        "cwd": str(repo_root),
        "timeout_sec": 10,
        "result_schema": {
            "primary_metric": "accuracy",
            "required_metrics": ["accuracy"],
            "metric_directions": {"accuracy": "higher"},
        },
    }
    app = create_session(
        ResearchBrief(
            request_text=topic,
            objective=topic,
            requested_outputs=("experiments", "report"),
            asset_requests=({"locator": str(fixture), "role": "paper"},),
        ),
        root=new_research_session_root(args.output_root, topic),
        services=ResearchApplicationServices(
            max_results=2,
            max_chunks=20,
            config={"execution": execution, "report": {"reviewer": "disabled", "max_review_iterations": 0}},
            budget_limits={"process_invocations": 1, "process_wall_seconds": 20},
        ),
    )

    view = app.view()
    for _ in range(32):
        if view.next_action == "report_write":
            break
        if view.next_action is None or view.status in {"paused", "blocked", "completed", "failed"}:
            break
        view = app.advance(max_actions=1)
    if view.next_action != "report_write":
        print(f"Research prefix stopped at {view.status}/{view.next_action}: {view.status_reason}")
        return 1
    search = app.controller.store.read_json(view.state_refs["search"])
    selected_ids = search.get("selected_paper_ids") or []
    papers = search.get("papers") or []
    paper_id = str(selected_ids[0]) if selected_ids else (
        str(papers[0].get("id")) if papers and isinstance(papers[0], dict) else ""
    )
    if not paper_id:
        print("Search smoke did not produce a selected paper id.")
        return 1
    app.services = replace(app.services, llm_client=FixtureReportWriter(paper_id))
    view = app.advance(max_actions=3)
    app.export_session()

    print(f"Session: {view.session_root}")
    print(f"Research status: {view.status}")
    print(f"Report status: {view.status}")
    required = ("plan", "search", "documents", "read", "synthesis", "design", "experiment", "analysis", "report", "report_audit")
    missing = [name for name in required if name not in view.state_refs]
    if missing:
        print("Missing canonical state refs: " + ", ".join(missing))
        return 1
    if not (view.session_root / "session_manifest.json").is_file():
        print("Missing canonical application manifest.")
        return 1
    return 0 if view.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
