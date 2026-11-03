"""Report delivery rules for the operation loop.

These checks consume actual delivery facts, not Writer composition identities.
Saved canonical-report contracts live in legacy_contract instead. Inspection and
publication owners provide their respective proofs; this owner decides actual
operation delivery eligibility without scheduling work or storing acceptance state.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from simple_ar.core.assessments import assessment_issues
from simple_ar.tools.delivery_review import delivery_requires_review, prepare_delivery_review
from simple_ar.tools.workspace import DeliveryRequest


def operation_report_admission(request: DeliveryRequest, requirement: dict | None) -> list[str]:
    """Check a frozen obligation before admitting a prepared proposed delivery.

    Partial delivery retains its existing escape from completeness checks; its
    actual files must still be resolved and byte-bound by the common executor.
    """
    if not request.unresolved and requirement is not None and request.report is None:
        return ['Frozen report requirement needs an explicit report claim or disclosed partial delivery']
    return []


def operation_report_content(request: DeliveryRequest, artifacts: list[str], *,
                             workspace: Any, requirement: dict | None,
                             legacy_report_status: bool) -> tuple[list[str], list[str]]:
    """Return frozen-content issues and manuscripts needing independent review.

    ``artifacts`` is the workspace owner's resolved inventory, including any
    registered outputs from the just-completed producer. No authoring receipt
    or canonical directory name is required on the operation contract.

    ``legacy_report_status`` is selected only from the loaded task contract by
    the delivery owner, never from model arguments. Saved pre-v4 tasks retain their
    canonical-only admission; v4 submits either edition for owner acceptance.
    """
    report = request.report
    if request.unresolved or report is None:
        return [], []
    if legacy_report_status and (report.status != 'canonical' or not report.manuscript):
        raise ValueError('A candidate report must disclose unfinished work')
    if not report.manuscript:
        raise ValueError('A report delivery must identify its actual editable manuscript')
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


class Delivery:
    """Own operation delivery eligibility; delegate evidence and publication proofs.

    No cached inventory or acceptance state. Each admission/review/terminal call
    uses its own current files; cross-stage freshness checks remain mandatory.
    Saved canonical Writer contracts remain in legacy_contract.
    """

    def __init__(self, *, workspace: Any, gateway: Any, publication: Any,
                 review_owner: Any, config: dict,
                 observations: Callable[[], list[dict]],
                 user_requests: Callable[[], list[str]] | None) -> None:
        self.workspace, self.gateway = workspace, gateway
        self.publication, self.review_owner = publication, review_owner
        self.config, self.observations, self.user_requests = config, observations, user_requests
        self.legacy_report_status = config.get('delivery_review_protocol') != 'task.v4'

    def issues(self, observations: list[dict], delivery: dict, *, proposed=None):
        request = DeliveryRequest.model_validate(delivery)
        if request.unresolved:
            return []  # Existing terminal binding still proves actual partial files.
        admission = operation_report_admission(request, self.config.get('report_requirement'))
        if admission:
            return admission
        spec = self.gateway.registry.get(proposed.tool_name) if proposed is not None else None
        producing = spec is not None and spec.completion is not None and bool(spec.completion.field)
        # A producer's declared delivery is an intention. Its owner admits the
        # request and binds the actual inventory; all acceptance checks below run
        # again on that retained result before the common terminal hash commit.
        if producing:
            return []
        resolved = self.workspace.resolve_delivery(request)
        return self._actual_issues(observations, request, resolved, proposed=proposed)

    def requires_review(self, request: DeliveryRequest) -> bool:
        return (self.review_owner is not None
                and delivery_requires_review(request, self.review_owner.protocol))

    def _actual_issues(self, observations: list[dict], request: DeliveryRequest,
                         resolved: dict, *, proposed=None,
                         include_inspection: bool = True) -> list[str]:
        """Judge actual production using this call's authorized resolved inventory."""

        issues = self.gateway.delivery_issues(observations, proposed=proposed,
            delivery=resolved)
        # task.v4 binds current files and delegates explicit computation adoption
        # to domain owners and applicability to DeliveryReview. Old process path
        # ownership cannot re-certify an ordinary edited delivery. Saved contracts
        # retain both their producer gate and its publication-input exception.
        if self.config.get('delivery_review_protocol') != 'task.v4':
            try:
                conversion_inputs = self.publication.conversion_inputs(observations, resolved['artifacts'])
                self.workspace.check_artifact_producers(path for path in resolved['artifacts']
                    if self.workspace.resolve(path) not in conversion_inputs)
            except (ValueError, OSError, KeyError, RuntimeError) as exc:
                issues.append(str(exc))
        from simple_ar.tools.workspace import missing_delivery_files
        missing = missing_delivery_files(self.workspace,resolved['artifacts'],self.config.get('delivery_files') or [])
        if missing:
            issues.append('Delivery is missing required workspace outputs: '+', '.join(missing))
        try:
            report_issues, required = operation_report_content(request, resolved['artifacts'],
                workspace=self.workspace, requirement=self.config.get('report_requirement'),
                legacy_report_status=self.legacy_report_status)
        except ValueError as exc:
            return [str(exc)]
        issues.extend(report_issues)
        scoped_review = self.requires_review(request)
        package_review = scoped_review and self.review_owner.protocol in {'task.v3', 'task.v4'}
        # Admission need not already have the review it is about to perform.
        # The whole-work owner reconsiders retained local opinions against this
        # exact claim and inventory; a second local gate would force duplicate
        # reviews or preserve a rejected old opinion forever. Historical file
        # contracts keep their gate. A package pass is never a local file pass.
        if include_inspection and not package_review and not (scoped_review and proposed is not None):
            issues.extend(assessment_issues(self.workspace,observations,resolved['artifacts'],
                require_pass=[] if scoped_review else required))
        if include_inspection and scoped_review and proposed is None:
            try:
                binding = prepare_delivery_review(self.workspace, request, resolved['artifacts'],
                    self.user_requests, protocol=self.review_owner.protocol)
                issues.extend(self.review_owner.issues(observations, binding))
            except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
                issues.append('Delivery review cannot be verified: ' + str(exc))
        issues.extend(operation_report_publication(request, resolved['artifacts'], observations,
            publication=self.publication, proposed=proposed))
        return issues

    def validate_actual(self, delivery: dict | DeliveryRequest, observations: list[dict], *,
                        current_outputs: tuple[Path, ...] = (),
                        include_inspection: bool = False) -> dict:
        """Resolve and validate completed production, returning the actual inventory.

        Post-production binding can replace its operation report validation with
        this call, passing the current producer observation and registered outputs.
        No proposed action can excuse missing production here. Default validation
        precedes review; terminal acceptance must set include_inspection=True.
        Partial delivery still resolves real files but claims no complete review.
        This API does not replace saved canonical Writer contract validation.
        """
        request = delivery if isinstance(delivery, DeliveryRequest) else DeliveryRequest.model_validate(delivery)
        admission = operation_report_admission(request, self.config.get('report_requirement'))
        if admission:
            raise ValueError('; '.join(admission))
        resolved = self.workspace.resolve_delivery(request, current_outputs=current_outputs)
        if not request.unresolved:
            issues = self._actual_issues(observations, request, resolved,
                include_inspection=include_inspection)
            if issues:
                raise ValueError('; '.join(dict.fromkeys(issues)))
        return resolved

    def review_delivery(self, delivery: dict, identity: str, *, may_start: bool,
                        current_result: dict | None = None,
                        current_outputs: tuple[Path, ...] = ()) -> dict | None:
        if self.review_owner is None:
            return None
        request = DeliveryRequest.model_validate(delivery)
        if request.unresolved:
            return None  # The executor still binds and commits actual partial files.
        if not may_start and not self.requires_review(request):
            return None  # Historical noncanonical claims had no review binding.
        if may_start:
            # Reconsider prior opinions only in the independent review. Actual
            # content and publication obligations must hold before paid work.
            observations = [*self.observations(), *([current_result] if current_result is not None else [])]
            try:
                resolved = self.validate_actual(request, observations, current_outputs=current_outputs)
            except ValueError as exc:
                raise ValueError('Delivery review not started: ' + str(exc)) from exc
        else:
            resolved = self.workspace.resolve_delivery(request, current_outputs=current_outputs)
        binding = prepare_delivery_review(self.workspace, request, resolved['artifacts'],
            self.user_requests, protocol=self.review_owner.protocol)
        return self.review_owner.review(binding, identity, may_start=may_start)
