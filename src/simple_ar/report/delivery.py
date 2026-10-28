"""Report delivery rules for the operation loop.

These checks consume actual delivery facts, not Writer composition identities.
Saved canonical-report contracts live in legacy_contract instead. Inspection,
publication receipts and the application still own their respective proofs and
execution; this module neither schedules work nor stores acceptance state.
"""
from __future__ import annotations

from typing import Any

from simple_ar.tools.workspace import DeliveryRequest


def operation_report_admission(request: DeliveryRequest, requirement: dict | None) -> list[str]:
    """Check a frozen obligation before admitting an intended final producer.

    Partial delivery retains its existing escape from completeness checks; its
    actual files must still be resolved and byte-bound by the common executor.
    """
    if not request.unresolved and requirement is not None and request.report is None:
        return ['Frozen report requirement needs an explicit report claim or disclosed partial delivery']
    return []


def operation_report_content(request: DeliveryRequest, artifacts: list[str], *,
                             workspace: Any, requirement: dict | None) -> tuple[list[str], list[str]]:
    """Return frozen-content issues and manuscripts needing independent review.

    ``artifacts`` is the workspace owner's resolved inventory, including any
    registered outputs from the just-completed producer. No authoring receipt
    or canonical directory name is required on the operation contract.
    """
    report = request.report
    if request.unresolved or report is None:
        return [], []
    if report.status != 'canonical' or not report.manuscript:
        raise ValueError('A candidate report must disclose unfinished work')
    manuscript = workspace.resolve(report.manuscript)
    if manuscript not in {workspace.resolve(path) for path in artifacts}:
        raise ValueError('Deliver the actual editable manuscript identified by the report claim')
    issues = []
    if requirement is not None:
        from simple_ar.report.writing import validate_output_outline
        from simple_ar.report.citations import body_citation_ids
        markdown = manuscript.read_text(encoding='utf-8')
        try:
            validate_output_outline(requirement['outline'], markdown)
        except ValueError as exc:
            issues.append(str(exc))
        limit = requirement['config'].get('max_cited_sources', 0)
        if limit and len(body_citation_ids(markdown, set(), include_unknown=True)) > limit:
            issues.append('Report exceeds frozen max_cited_sources')
    return issues, [report.manuscript]


def operation_report_publication(request: DeliveryRequest, artifacts: list[str],
                                 observations: list[dict], *, publication: Any,
                                 proposed: Any = None) -> list[str]:
    """Ask the publication owner to prove only applicable requested formats."""
    if request.unresolved or request.report is None:
        return []
    report = request.report
    try:
        publication.check_requested_delivery(observations, artifacts,
            formats=report.formats, manuscript=report.manuscript,
            proposed=proposed.arguments if proposed is not None and proposed.tool_name == 'export_report' else None)
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
        return [str(exc)]
    return []
