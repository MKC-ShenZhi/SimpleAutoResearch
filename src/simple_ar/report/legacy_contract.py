"""Report semantics for saved canonical-report contracts.

No scheduling or durable state: callers supply accepted observations and the
existing writing/publication validators. New operation tasks do not acquire
these historical composition requirements.
"""
from __future__ import annotations
from typing import Any, Callable, Iterable
from simple_ar.tools.writing import ProfessionalReview

_REPORT_PROTOCOL = 'canonical_report.v1'
_DECLARED_INPUTS = 'declared_inputs.v1'

def project_report(config: dict, observations: Iterable[dict]) -> dict[str, Any]:
    """Effective obligation from frozen configuration and accepted plan receipts."""
    declaration = None
    if config.get('report_protocol') == _REPORT_PROTOCOL:
        for row in observations:
            if row.get('tool_name') == 'update_work_plan' and row.get('status') == 'ok' and row.get('data', {}).get('report_intent'):
                declaration = row['data']
                break
    intent = declaration['report_intent'] if declaration else None
    requirement = config.get('report_requirement') or (declaration or {}).get('report_requirement')
    kind = ('conversion' if intent and intent['kind'] == 'conversion' else
        'report' if config.get('report_requirement') else intent['kind'] if intent else 'pending')
    execution = (config.get('code_task') is not None or config.get('execution_contract') is not None
        or (config.get('research_policy') or {}).get('require_design', False))
    evidence_contract = config.get('report_evidence_contract')
    # Only a new dynamic obligation needs a default. Frozen/legacy reports
    # without this field historically require execution evidence.
    if (evidence_contract is None and config.get('report_protocol') == _REPORT_PROTOCOL
            and not config.get('report_requirement')):
        evidence_contract = 'execution.v1' if execution else 'materials.v1'
    return {'kind': kind, 'requirement': requirement,
        'evidence_contract': evidence_contract,
        'formats': list(dict.fromkeys(['markdown', *(intent or {}).get('formats', [])])) if requirement
            else (intent or {}).get('formats', []),
        'intent': intent, 'conversion_source': (declaration or {}).get('conversion_source')}


def declare_report(intent: Any, *, config: dict, requests: list[str], previous: Callable[[], dict],
                   writing: Any, report_inputs: Callable[[], dict | None]) -> dict:
    if config.get('report_protocol') != _REPORT_PROTOCOL:
        raise ValueError('Report intent requires the explicitly versioned report protocol')
    if (intent.request_index != len(requests) - 1 or not intent.quote.strip()
            or intent.quote not in requests[intent.request_index]):
        raise ValueError('Report intent must quote the current actual user request at its zero-based index')
    previous = previous()
    old = previous['intent']
    data = intent.model_dump(mode='json')
    frozen = config.get('report_requirement')
    if frozen and (intent.kind == 'none' or intent.method not in {None, frozen['method']}):
        raise ValueError('Frozen report configuration takes precedence over a plan declaration')
    if old and old['kind'] != 'none':
        if intent.kind != old['kind'] and intent.request_index <= old['request_index']:
            raise ValueError('Changing report purpose requires newer quoted user semantics')
        if intent.replace:
            if intent.request_index <= old['request_index']:
                raise ValueError('Replacing a report obligation requires newer quoted user semantics')
        else:
            if intent.kind != old['kind'] and intent.kind != 'conversion':
                raise ValueError('A plan cannot clear an inherited report obligation; quote newer user semantics and explicitly replace')
            if intent.method and previous['requirement'] and intent.method != previous['requirement']['method']:
                raise ValueError('Changing the inherited writing method requires newer user semantics and replace')
            data['formats'] = list(dict.fromkeys([*old['formats'], *data['formats']]))
            data['method'] = intent.method or old.get('method')
    requirement = config.get('report_requirement')
    requested_review = intent.professional_review
    retained_requirement = requirement or (previous['requirement'] if old and not intent.replace else None)
    if requested_review is None and retained_requirement is None and writing.document_work and data['kind'] == 'report':
        # Ordinary complete work includes one bounded correction, not an
        # inspection-only default that makes users configure the editor.
        # Explicit/frozen controls and all existing obligations prevail.
        requested_review = ProfessionalReview(max_revision_rounds=1)
    if requested_review is not None:
        if data['kind'] != 'report':
            raise ValueError('Professional writing review requires a report obligation')
        if retained_requirement:
            controls = retained_requirement['config']
            if (controls['reviewer'] != 'llm' or not controls['document_review']
                    or controls['max_review_iterations'] != requested_review.max_revision_rounds):
                raise ValueError('Professional review cannot replace inherited or user-frozen controls; use newer user semantics and replace for a dynamic obligation')
    if not requirement and data['kind'] == 'conversion':
        requirement = previous['requirement']
    if not requirement and data['kind'] == 'report':
        from simple_ar.tools.writing import freeze_report_requirement
        requirement = (previous['requirement'] if old and not intent.replace else None) or freeze_report_requirement(
            {'reviewer': 'llm' if requested_review is not None else 'disabled',
             'document_review': requested_review is not None,
             'max_review_iterations': requested_review.max_revision_rounds if requested_review is not None else 0,
             **({'review_scope': 'document', 'draft_scope': 'document'} if requested_review is not None else {}),
             'allow_source_backtracking': False, 'allow_llm_fallback': False, 'figures': {'enabled': False}},
            method=data['method'] or 'article')
    if requirement and data['method'] not in {None, requirement['method']}:
        raise ValueError('A declaration cannot change the effective writing method without replacing its report requirement')
    conversion = None
    if intent.kind == 'conversion':
        if not intent.source:
            raise ValueError('Conversion must explicitly adopt an existing manuscript source')
        conversion = writing.publication_source(intent.source,
            report_inputs() if previous['requirement'] else None, intent=data)
        if previous['requirement'] and not conversion.get('composition'):
            raise ValueError('An inherited report conversion must adopt its completed canonical manuscript, not replacement prose')
        if old and old['kind'] == 'conversion' and not intent.replace and conversion != previous['conversion_source']:
            raise ValueError('Changing the adopted conversion source requires newer user semantics and replace')
    elif intent.source is not None:
        raise ValueError('Only conversion declarations adopt an existing manuscript source')
    return {'report_intent': data, **({'report_requirement': requirement} if requirement else {}),
        **({'conversion_source': conversion} if conversion else {})}


def validate_report_delivery(*, config: dict, report: dict, observations: Callable[[], list[dict]],
                             artifacts: list[str], workspace: Any, writing: Any,
                             publication: Any, report_inputs: Callable[[], dict | None],
                             claim: Any = None, partial: bool = False,
                             publication_result: dict | None = None,
                             proposed_export: dict | None = None) -> None:
    versioned = config.get('report_protocol') == _REPORT_PROTOCOL
    if versioned:
        if report['intent'] is None and config.get('report_requirement') is None:
            raise ValueError('Report intent is undetermined; declare report, conversion or none from the actual request before delivery')
        if report['kind'] == 'none':
            return
        if claim is None:
            raise ValueError('Declare delivery.report as candidate or canonical; unresolved alone does not establish report identity')
        if claim.status == 'candidate':
            if not partial:
                raise ValueError('A candidate delivery must explicitly disclose unresolved report work')
            return
    observations = observations()
    if versioned and report['kind'] == 'conversion':
        if not claim.manuscript or not report['conversion_source']:
            raise ValueError('Conversion delivery must identify its explicitly adopted manuscript')
        identity = writing.publication_source(claim.manuscript, report_inputs(),
            intent=report['intent'], adopted=report['conversion_source'])
        if identity is not None and workspace.resolve(identity['manuscript']) not in {
                workspace.resolve(path) for path in artifacts}:
            raise ValueError('Include the unchanged canonical manuscript in the conversion delivery')
    elif report['requirement'] is not None:
        identity = writing.report_delivery(observations, report_inputs(), artifacts,
            manuscript=claim.manuscript if claim else None,
            intent=report['intent'])
    else:
        raise ValueError('No current canonical report obligation')
    if versioned:
        published = publication.check_report_delivery(observations + ([publication_result] if publication_result else []), identity,
            artifacts, formats=report['formats'], manuscript=claim.manuscript, proposed=proposed_export)
        if (config.get('professional_work') in {'outcomes.v2', 'owners.v1', 'owners.v2', 'owners.v3', 'requests.v1'}
                and report['kind'] != 'conversion'):
            writing.check_delivery_set(identity, artifacts, published)


def bind_report_inputs(*, config: dict, report: dict, workspace: Any, requests: list[str],
                       material_observations: Callable[[], list[dict]],
                       execution_evidence: Callable[..., dict], implementation: Any) -> dict | None:
    if config.get('report_protocol') == _REPORT_PROTOCOL and report['kind'] == 'pending':
        raise ValueError('Report intent is undetermined; declare report, conversion or none before Writer consumption in the same action batch')
    if report['requirement'] is None:
        return None
    if report['evidence_contract'] == 'materials.v1':
        from simple_ar.tools.writing import material_report_binding
        observations = material_observations()
        mounts = config['sources']
        table = config.get('table_analysis')
        if table and any(row.get('tool_name') == 'analyze_table' for row in observations):
            # The deterministic owner already froze the raw input into its
            # portable package. Report feedback binds that actual snapshot,
            # not a permanent dependency on the original external CSV.
            mounts = [name for name in mounts if name != table['path']]
        binding = material_report_binding(workspace, report['requirement'],
            mounts=mounts, observations=observations,
            requests=requests)
    else:
        evidence = execution_evidence(require_current_origin=report['kind'] != 'conversion')
        if not implementation.exact and not evidence.get('analysis_records'):
            raise ValueError('A measurement report requires the matching deterministic analysis before composition')
        binding = {**evidence, 'requirement': report['requirement']}
    if config.get('report_protocol') == _REPORT_PROTOCOL:
        binding['report_protocol'] = _REPORT_PROTOCOL
        binding['obligation'] = {'intent': report['intent'], 'formats': report['formats']}
    if config.get('input_contract') == _DECLARED_INPUTS:
        binding['input_contract'] = _DECLARED_INPUTS
        binding['creation_declarations'] = config['creation_declarations']
    return binding
