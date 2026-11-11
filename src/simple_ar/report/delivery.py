"""Report delivery rules for the operation loop.

These checks consume actual delivery facts, not Writer composition identities.
Saved canonical-report contracts live in legacy_contract instead. Inspection and
publication owners provide their respective proofs; this owner decides actual
operation delivery eligibility without scheduling work or storing acceptance state.
"""
from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable

from simple_ar.core.assessments import assessment_issues
from simple_ar.tools.delivery_review import delivery_requires_review, prepare_delivery_review
from simple_ar.tools.specs import tool_completion
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
        self.legacy_report_status = config.get('delivery_review_protocol') not in {'task.v4', 'task.v5'}

    def working_guidance(self, accepted_delivery: dict | None, *, include_lifecycle: bool,
                         observations: list[dict] | None = None) -> tuple[dict | None, dict | None]:
        """Project one decision's guidance from the same observations and review."""
        observations = self.observations() if observations is None else observations
        reviewed: list[tuple[dict, list[str]]] = []

        def review_issues(rows: list[dict], binding: dict) -> list[str]:
            for previous, issues in reviewed:
                if binding == previous:
                    return issues
            issues = self.review_owner.issues(rows, binding)
            reviewed.append((binding, issues))
            return issues

        lifecycle = (self.lifecycle_requirements(observations=observations, review_issues=review_issues,
                         accepted_delivery=accepted_delivery)
                     if include_lifecycle else None)
        return lifecycle, self.feedback(observations, accepted_delivery, review_issues=review_issues)

    def feedback(self, observations: list[dict], accepted_delivery: dict | None, *, review_issues=None) -> dict | None:
        """Reconsider the latest actual proposal through the same eligibility rules."""
        for row in reversed(observations):
            contract = tool_completion(row.get('tool_name', ''), self.config.get('tool_completions'))
            if row.get('status') != 'ok' or contract is None or contract.kind not in {'delivery', 'review'}:
                continue
            data = row.get('data') or {}
            delivery = data.get(contract.field) if contract.field else data
            if delivery is None:
                continue
            if delivery == accepted_delivery:
                return None
            try:
                issues = self.issues(observations, delivery, review_issues=review_issues)
            except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
                issues = [str(exc)]
            return {'record': 'records/' + row['record'].removeprefix('attempts/'),
                'request_count': row.get('request_count', 1),
                'status': 'rejected' if issues else 'conditions_met', 'issues': issues,
                'scope': 'Current check of the previous delivery proposal; execution success is not acceptance. '
                    'Preserve completed work; the current user request determines whether to continue or replace this proposal. '
                    'This grants no permission and does not add requirements to a new goal.'}
        return None

    def issues(self, observations: list[dict], delivery: dict, *, proposed=None, review_issues=None):
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
        return self._actual_issues(observations, request, resolved, proposed=proposed, review_issues=review_issues)

    def requires_review(self, request: DeliveryRequest) -> bool:
        return (self.review_owner is not None
                and delivery_requires_review(request, self.review_owner.protocol))

    def lifecycle_requirements(self, *, observations: list[dict] | None = None, review_issues=None,
                               accepted_delivery: dict | None = None) -> dict:
        """Derive remaining obligations from original proposals and current bytes.

        This is working guidance, never acceptance or another saved task state.
        The resource floor is only the first independent decision's lower bound;
        inspection still admits and accounts its actual investigation itself.
        """
        from simple_ar.tools.specs import tool_completion

        conditions = []
        prior_unresolved = []
        next_tools = ['review_result', 'deliver_result'] if self.review_owner else ['deliver_result']
        proposal = None
        previously_delivered = False
        delivered_current_request = False
        observations = self.observations() if observations is None else observations
        work_after_review = False
        review_followup = False
        for observed in reversed(observations):
            name = observed.get('tool_name', '')
            completion = tool_completion(name, self.config.get('tool_completions'))
            spec = self.gateway.registry.get(name)
            changed = (observed.get('status') == 'ok' and spec is not None
                and spec.permission_level in {'write_patch', 'execution'})
            if completion is None or completion.kind not in {'delivery', 'review'}:
                work_after_review |= changed
                continue
            data = observed.get('data') or {}
            value = data.get(completion.field) if completion.field else data
            try:
                proposal = DeliveryRequest.model_validate(value)
            except (ValueError, TypeError):
                work_after_review |= changed
                continue
            if (completion.kind == 'delivery' and observed.get('status') == 'ok'
                    and value == accepted_delivery):
                prior_unresolved = list(proposal.unresolved)
                proposal = None
                previously_delivered = True
                delivered_current_request = observed.get('current_request', True)
                break
            review_followup = (completion.kind == 'review' and observed.get('status') == 'ok'
                and observed.get('current_request', True) and not work_after_review)
            break
        # A new request still needs the saved final-review contract. A delivery
        # already accepted for this request has no outstanding review floor.
        needs_review = self.review_owner is not None and not (
            previously_delivered and delivered_current_request)
        if proposal is None:
            conditions.append({'kind': 'candidate_submission',
                'message': ('The previous delivery is recorded. Submit a new candidate when the '
                    'current work is ready.' if previously_delivered else
                    'No candidate or delivery has been submitted. An applicable independent '
                    'check may investigate useful work before final delivery; a final submission '
                    'can also request the necessary check.')})
        else:
            prior_unresolved = list(proposal.unresolved)
            try:
                resolved = self.workspace.resolve_delivery(proposal)
                # Unfinished work can be inspected before publication. It must
                # not hide the remaining publication owner from planning.
                publication = operation_report_publication(proposal.model_copy(update={'unresolved': []}), resolved['artifacts'],
                    observations, publication=self.publication)
                if publication:
                    next_tools.append('export_report')
                    conditions.extend({'kind': 'publication', 'message': text} for text in publication)
                if self.review_owner and not proposal.unresolved:
                    binding = prepare_delivery_review(self.workspace, proposal, resolved['artifacts'],
                        self.user_requests, protocol=self.review_owner.protocol)
                    concerns = (review_issues or self.review_owner.issues)(observations, binding)
                    needs_review = bool(concerns)
                    conditions.extend({'kind': 'independent_review', 'message': text} for text in concerns)
            except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
                conditions.append({'kind': 'candidate_versions', 'message': str(exc)})
        return {'conditions': conditions, 'prior_unresolved': prior_unresolved, 'next_tools': next_tools,
            'minimum_resources': {'llm_requests': int(needs_review)},
            'review_followup': review_followup,
            'scope': 'Conditions are current owner checks; prior_unresolved is the previous producer\'s '
                'statement, not a current obligation. Reconsider it against the request and actual work. '
                'This is not a pass or an exact cost forecast. '
                'Final acceptance rechecks the actual inventory, publication and applicable opinion.'}

    def review_continuation(self, decision: dict) -> dict[str, dict]:
        """Leave a decision to act on an opinion and a request for its exit.

        A revision or another check may be necessary, but their cost and even
        their need are not known before the opinion. The follow-up can deliver
        an accepted candidate or choose the next action for a finding. Keep a
        request slot to report a revision, but assess its token cost when that
        request is prepared instead of forecasting a second full context now.
        """
        return {'followup': dict(decision), 'exit': {'llm_requests': 1}}

    def _actual_issues(self, observations: list[dict], request: DeliveryRequest,
                         resolved: dict, *, proposed=None,
                         include_inspection: bool = True, review_issues=None) -> list[str]:
        """Judge actual production using this call's authorized resolved inventory."""

        issues = self.gateway.delivery_issues(observations, proposed=proposed,
            delivery=resolved)
        # task.v4 binds current files and delegates explicit computation adoption
        # to domain owners and applicability to DeliveryReview. Old process path
        # ownership cannot re-certify an ordinary edited delivery. Saved contracts
        # retain both their producer gate and its publication-input exception.
        if self.config.get('delivery_review_protocol') not in {'task.v4', 'task.v5'}:
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
        package_review = scoped_review and self.review_owner.protocol in {'task.v3', 'task.v4', 'task.v5'}
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
                issues.extend((review_issues or self.review_owner.issues)(observations, binding))
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
                        current_outputs: tuple[Path, ...] = (), candidate: bool = False) -> dict | None:
        if self.review_owner is None:
            return None
        request = DeliveryRequest.model_validate(delivery)
        if request.unresolved and not candidate:
            return None  # The executor still binds and commits actual partial files.
        if candidate and not request.unresolved:
            # A fully eligible claim keeps the normal reusable final binding.
            # Only unfinished admission needs a distinct early investigation.
            observations = [*self.observations(), *([current_result] if current_result is not None else [])]
            try:
                self.validate_actual(request, observations, current_outputs=current_outputs)
            except ValueError as exc:
                if may_start and request.report is not None and request.report.status == 'canonical':
                    raise ValueError('Candidate declared ready for delivery but is incomplete: ' + str(exc)) from exc
            else:
                candidate = False
        if not may_start and not candidate and not self.requires_review(request):
            return None  # Historical noncanonical claims had no review binding.
        if may_start:
            # Reconsider prior opinions only in the independent review. Actual
            # content and publication obligations must hold before paid work.
            observations = [*self.observations(), *([current_result] if current_result is not None else [])]
            try:
                resolved = (self.workspace.resolve_delivery(request, current_outputs=current_outputs)
                    if candidate else self.validate_actual(request, observations, current_outputs=current_outputs))
            except ValueError as exc:
                raise ValueError('Delivery review not started: ' + str(exc)) from exc
        else:
            resolved = self.workspace.resolve_delivery(request, current_outputs=current_outputs)
        binding = prepare_delivery_review(self.workspace, request, resolved['artifacts'],
            self.user_requests, protocol=self.review_owner.protocol, candidate=candidate)
        return self.review_owner.review(binding, identity, may_start=may_start)

    def bind_result(self, request: DeliveryRequest, payload: dict, identity: str, *,
                    current_outputs: tuple[Path, ...], current_result: dict,
                    retain: Callable[[], None], review_capacity: Callable[[], Any],
                    may_start: bool = False, candidate: bool = False) -> dict:
        """Bind completed production and applicable review to one exact claim.

        The executor supplies receipt persistence and physical resource protection;
        this owner alone interprets summary, inventory and review applicability.
        Neither an inspection opinion nor this projection commits the task.
        """
        delivery = self.workspace.resolve_delivery(request, current_outputs=current_outputs)
        hashes = self.workspace.artifact_hashes(delivery['artifacts'])
        metadata = payload.setdefault('metadata', {})
        retained = metadata.get('delivery_hashes')
        if retained is not None and hashes != retained:
            raise ValueError('Final work outputs changed before delivery binding; no delivery or replay')
        package_review = (self.review_owner is not None
            and self.review_owner.protocol in {'task.v3', 'task.v4', 'task.v5'})
        if package_review and request.summary_source is not None:
            delivery['summary'] = self.workspace.delivery_summary(delivery, hashes)
        try:
            if may_start and self.review_owner is not None:
                metadata['delivery_hashes'] = hashes
                retain()  # Pin production before paid work; recovery never reruns it.
            review = self.review_delivery(delivery, identity, may_start=False,
                current_result=current_result, current_outputs=current_outputs, candidate=candidate)
            if review is None and may_start:
                with review_capacity() if candidate or self.requires_review(request) else nullcontext():
                    review = self.review_delivery(delivery, identity, may_start=True,
                        current_result=current_result, current_outputs=current_outputs, candidate=candidate)
            if review is not None:
                if metadata.get('delivery_review', review) != review:
                    raise ValueError('Retained delivery review differs from its actual receipt')
                metadata['delivery_review'] = review
                if not review.get('error'):
                    reviewed = review['result']['metadata']
                    for key in (('inspection_binding',) if package_review
                                else ('assessments', 'inspection_binding')):
                        if key in reviewed:
                            metadata[key] = reviewed[key]
                    if review['identity'] == identity:
                        artifacts = metadata.setdefault('capability_artifacts', [])
                        artifacts.extend(ref for ref in reviewed.get('capability_artifacts', [])
                            if ref not in artifacts)
            if hashes != self.workspace.artifact_hashes(delivery['artifacts']):
                raise ValueError('Delivery changed during its independent review')
        except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
            metadata['delivery_review_error'] = str(exc)
        if request.summary_source is not None and not package_review:
            delivery['summary'] = self.workspace.delivery_summary(delivery, hashes)
        metadata['delivery_hashes'] = hashes
        return delivery
