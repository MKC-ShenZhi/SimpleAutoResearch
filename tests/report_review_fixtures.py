"""Literal draft anchors for synthetic orchestration clients, not fact proofs."""
import json

from simple_ar.report.schema import (ReportContext, ReportMemory, ReportRuntimeConfig,
    ReportSectionPlan, ReportSectionDraft, ReviewerFinding, ReportDraftQuote)
from simple_ar.report.templates import load_report_template_bundle


def finding_check_objects(*, historical=True, finding_changes=None, **settings):
    """Fresh report and historical opinion inputs; no test-case dependency."""
    context = ReportContext(topic="Recorded observations", report_mode="experiment")
    old = ReviewerFinding(finding_id="old-assumption", section_id="scope",
        type="unsupported_claim", severity="major", required_action="revise",
        message="An earlier reviewer treated an unverified interpretation as fact.",
        draft_quotes=[ReportDraftQuote(section_id="scope", quote="The identity was not independently verified.")])
    if finding_changes:
        old = old.model_copy(update=finding_changes)
    findings = [old] if historical else []
    memory = ReportMemory(section_plan=[ReportSectionPlan(section_id=sid, heading=sid,
        goal="Explain the evidence", draft_order=index, final_order=index)
        for index, sid in enumerate(("scope", "limits"), start=1)], reviewer_findings=findings)
    config = ReportRuntimeConfig(**{"document_review": True, "max_review_iterations": 0, **settings})
    drafts = [ReportSectionDraft(section_id=sid, heading=sid,
        draft_markdown="The identity was not independently verified.") for sid in ("scope", "limits")]
    checkpoint = {"memory": memory.model_dump(mode="json"),
        "sections": [row.model_dump(mode="json") for row in drafts], "iterations": [],
        "reviewer_findings": [row.model_dump(mode="json") for row in findings], "tool_results": [],
        "document_review_done": False, "pending_draft": None}
    kwargs = dict(context=context, memory=memory, config=config,
        template=load_report_template_bundle(report_mode="experiment", config=config))
    return kwargs, checkpoint, old, drafts


def document_review_inputs(memory, config, sections, *, template=None, **evidence):
    """Fresh request defaults; scenario evidence and callbacks stay explicit."""
    return dict(template=template if template is not None else
                load_report_template_bundle(report_mode="experiment", config=config),
                memory=memory, sections=sections, config=config,
                **{"execution_summary": {}, "metric_summary": {}, **evidence})


def opinion_check_response(finding, *, verdict="pass", **check):
    """One raw opinion answer; callers own status, explanation and literal anchors."""
    return {"section_reviews": [{"section_id": finding.section_id, "verdict": verdict,
            "finding_checks": [{"finding_id": finding.finding_id, **check}]}]}


def unresolved_opinion_response(view, *, verdict="revise_required",
                                explanation="This fixture does not establish a resolution."):
    """Prompt-capture fixtures still answer every requested opinion explicitly."""
    grouped = {}
    for row in view["historical_findings_to_check"]:
        review = grouped.setdefault(row["section_id"], {"section_id": row["section_id"],
            "finding_checks": []})
        if verdict is not None:
            review["verdict"] = verdict
        review["finding_checks"].append({"finding_id": row["finding_id"], "status": "unresolved",
            "explanation": explanation})
    return {"section_reviews": list(grouped.values())}


def prompt_payload(prompt):
    """Decode the JSON view after a protocol preamble, without altering evidence."""
    return json.JSONDecoder().raw_decode(prompt[prompt.index("{"):])[0]


def draft_quotes(prompt, section_id):
    view = prompt_payload(prompt)
    draft = view.get('draft', {})
    if draft.get('section_id') == section_id:
        text = draft['draft_markdown']
    else:
        text = next(row['markdown'] for row in view['sections'] if row['section_id'] == section_id)
    return [{'section_id': section_id, 'quote': text}]
