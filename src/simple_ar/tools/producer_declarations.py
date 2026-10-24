"""Producer evidence, source-bound notes and historical declaration adoption.

New work supplies original notes as inspection context, not extra completion
duties. Saved declaration contracts retain their original adoption checks.
Both paths use the existing producer/reviewer receipts and version owners;
neither rewrites original notes or introduces a second audit store.
"""
from __future__ import annotations

import hashlib
import re
from types import SimpleNamespace
from typing import Any

from simple_ar.core.capabilities import ArtifactStore


def _proof(owner: Any) -> Any:
    from simple_ar.tools.implementation import ImplementationToolGateway
    registered = getattr(owner, '_registered_ref', None)
    if not callable(registered):
        registered = lambda record, **kwargs: ImplementationToolGateway._registered_ref(owner, record, **kwargs)
    return SimpleNamespace(root=owner.root, workspace=owner.workspace, ledger=owner.ledger,
        session_id=getattr(owner, 'session_id', None),
        _registered_ref=registered)


def _producer_group(owner: Any, locator: str, producer: dict | None = None) -> dict | None:
    from simple_ar.tools.implementation_work import _accepted, _execution, _retained_request, _same_producer
    from simple_ar.tools.implementation_run import candidate_source
    proof = _proof(owner)
    saved, _ = _execution(proof, locator, successful=True)
    _, prepared, source = candidate_source(proof, saved['prepared_result'])
    request = _retained_request(source)
    completion = request.producer_completion
    if (not saved.get('unresolved') or completion is None
            and (request.work_contract != 'shared.v2' or not request.complete_work)):
        return None  # Historical declarations retain their original delivery gate.
    preparation = ArtifactStore(owner.workspace.resolve(saved['prepared_result']).parent.parent)
    _accepted(proof, preparation.read_json('native_result.json'), preparation.root.name)
    statements = prepared['data'].get('unresolved', [])
    if (statements != saved['unresolved'] or statements != saved['approval']['binding']['unresolved']
            or any(not isinstance(text, str) for text in statements)):
        raise ValueError('Producer declarations differ from their prepared and approved originals')
    versions = owner.workspace.artifact_provenance(saved['output_sha256'])
    actual = next(iter(versions.values()))['producer']
    if (not actual or any(not _same_producer(proof, row['producer'], actual) for row in versions.values())
            or producer is not None and not _same_producer(proof, producer, actual)):
        raise ValueError('Adopted declarations have no single current physical producer')
    execution = owner.workspace.resolve(locator)
    prepared_path = owner.workspace.resolve(saved['prepared_result'])
    group = {'execution_ref': f'records/{execution.parent.name}/candidate_execution.json',
        'execution_sha256': hashlib.sha256(execution.read_bytes()).hexdigest(),
        'prepared_result': saved['prepared_result'],
        'prepared_sha256': hashlib.sha256(prepared_path.read_bytes()).hexdigest(),
        'source_binding_sha256': hashlib.sha256((prepared_path.parent / 'request.json').read_bytes()).hexdigest(),
        'goal': request.goal, 'producer': actual,
        'declarations': [{'id': f'{execution.parent.name}:{index}', 'statement': text,
            'source_pointer': f'/unresolved/{index}'} for index, text in enumerate(statements)],
        'execution': {key: saved[key] for key in ('status', 'returncode', 'stop_reason',
            'argv', 'cwd', 'output_sha256', 'verification') if key in saved}}
    if completion == 'goal_findings.v1':
        group.pop('declarations')
        group.update(producer_completion=completion, notes=statements)
    return group


def declaration_group(owner: Any, locator: str, producer: dict | None = None) -> dict | None:
    group = _producer_group(owner, locator, producer)
    return group if group is not None and 'declarations' in group else None


def delivery_evidence(owner: Any, observations: list[dict], delivery: dict | None) -> tuple[list[dict], list[dict]]:
    """Select owners by delivered subjects and the registered dependency graph.

    No task names, path guesses or import analysis. The workspace already
    validates/traverses its declared process edges; reuse its checked cache.
    """
    from simple_ar.tools.implementation_work import _accepted, _execution
    from simple_ar.tools.workspace import DeliveryRequest
    preparations = [row for row in observations if row.get('tool_name') == 'prepare_implementation']
    executions = [row for row in observations if row.get('tool_name') == 'execute_implementation']
    if delivery is None:
        return preparations, executions  # No delivery scope cannot waive earlier work.
    request = DeliveryRequest.model_validate(delivery)
    subjects, source_inputs = set(), set()
    for locator in request.artifacts:
        path = owner.workspace.resolve(locator)
        if path.is_dir():
            expanded = owner.workspace.resolve_delivery(request.model_copy(update={'artifacts': [locator]}))
            subjects.update(owner.workspace.resolve(name) for name in expanded['artifacts'])
        else:
            subjects.add(path)
    delivered_subjects = set(subjects)
    # The writing owner distinguishes current delivery subjects from source
    # evidence. A prior draft's inputs can remain valid context without being
    # adopted as the revised deliverables. Preserve their physical checks.
    for observed in observations:
        data = observed.get('data') or {}
        if (observed.get('tool_name') != 'compose_document' or observed.get('status') != 'ok'
                or not data.get('editable_markdown')
                or owner.workspace.resolve(data['editable_markdown']) not in subjects):
            continue
        identity = owner.workspace.resolve(data['editable_markdown']).parent.name
        store = ArtifactStore(owner.root / 'tools/writing' / identity)
        _accepted(_proof(owner), {key: value for key, value in observed.items()
            if key not in {'arguments', 'record', 'current_request'}}, identity)
        saved, binding = store.read_json('result.json'), store.read_json('request.json')
        if (saved['data'] != data or saved['artifacts'] != observed.get('artifacts')
                or data.get('inputs') != binding['inputs']
                or owner.workspace.artifact_hashes(data['artifact_sha256']) != data['artifact_sha256']):
            raise ValueError('Delivered composition source/version changed before producer selection')
        if binding.get('delivery_review', {}).get('producer_completion') == 'goal_findings.v1':
            from simple_ar.tools.writing import read_composition_review
            from simple_ar.report.writing import validate_document_inspection
            review = read_composition_review(owner.workspace, data)
            if (review.get('review_status') != 'reviewed' or data.get('unresolved') or data.get('incomplete')
                    or data.get('delivery_review', {}).get('status') != 'reviewed'):
                raise ValueError('Delivered composition requires its current independent review to pass')
            validate_document_inspection(store, binding, data, review_result=review)
        subjects.update(owner.workspace.resolve(row['path'])
            for row in binding.get('delivery_review', {}).get('artifacts', []))
        source_inputs.update(owner.workspace.resolve(row['path']) for row in binding['inputs'])
    cache = {}
    owner.workspace.check_artifact_producers([str(path) for path in subjects | source_inputs], cache=cache)
    dependencies, invocation_ids, subject_producers = subjects | source_inputs, set(), set()
    for path in cache.get('checked', set()):
        _, record, binding = cache['producers'][path]
        invocation_ids.add(record['invocation_id'])
        if path in subjects:
            subject_producers.add(record['invocation_id'])
        dependencies.add(path)
        dependencies.update(owner.workspace.resolve(name) for name in binding['inputs'])
    # Applicability filters may retire coordinator observations, not ownership
    # of files that are actually delivered/consumed. Consult existing registered
    # preparation outputs in the core journal, never candidate directories or a
    # second ownership index. Settlement is checked only for relevant sources.
    known = {owner.workspace.resolve(row['data']['prepared_result']) for row in preparations
        if row.get('data', {}).get('prepared_result')}
    known_executions = {owner.workspace.resolve(row['data']['execution_ref']) for row in executions
        if row.get('data', {}).get('execution_ref')}
    session = ArtifactStore(owner.root).read_json('session_manifest.json')
    proof = _proof(owner)
    for identity in session['budget']['recorded_attempts']:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', identity):
            raise ValueError('Invalid registered preparation attempt identity')
        store = ArtifactStore(owner.root / 'attempts' / identity)
        manifest = store.read_attempt_manifest()
        if manifest.capability != 'native_tool' or manifest.status != 'completed':
            continue
        for output in manifest.outputs:
            contracts = {'prepared_implementation': ('prepared_implementation.v1', 'prepare_implementation', 'prepared_result'),
                'prepared_implementation_execution': ('prepared_implementation_execution.v1', 'execute_implementation', 'execution_ref')}
            if output.kind not in contracts or output.status != 'available':
                continue
            schema, tool_name, field = contracts[output.kind]
            if output.schema != schema:
                continue
            record = store.resolve(output.path)
            seen = known if tool_name == 'prepare_implementation' else known_executions
            if record in seen:
                continue
            proof._registered_ref(record, kind=output.kind, store_root=store.root)
            proof._registered_ref(store.root / 'native_result.json', kind='native_observation', store_root=store.root)
            observed = store.read_json('native_result.json')
            if (observed.get('tool_name') != tool_name
                    or owner.workspace.resolve(observed['data'][field]) != record):
                raise ValueError('Registered owner result differs from its accepted native observation')
            (preparations if tool_name == 'prepare_implementation' else executions).append(observed)
            seen.add(record)
    selected_preparations, preparation_refs, subject_preparations = [], set(), set()
    for observed in preparations:
        data = observed.get('data') or {}
        if observed.get('status') != 'ok' or not data.get('prepared_result'):
            continue
        result = owner.workspace.resolve(data['prepared_result'])
        candidate = owner.workspace.resolve(f'workspace/implementations/{result.parent.parent.name}')
        files = {owner.workspace.resolve(str(candidate / row['path'])) for row in data['files']}
        files.update(owner.workspace.resolve(str(candidate / name))
            for name in (data.get('execution_plan') or {}).get('outputs', []))
        # A retained design-context source from a superseded complete candidate
        # is not a delivered run. Actual subjects, outputs and source-only
        # dependencies still retain their own obligations.
        # A source file reviewed as an input of another producer does not
        # make its original complete-work run a current delivery obligation.
        # Explicitly delivering that source does; delivered generated outputs
        # are traced to their actual execution separately below.
        matched = bool(files.intersection(delivered_subjects))
        if matched:
            subject_preparations.add(result)
        if not matched and files.intersection(dependencies):
            source = ArtifactStore(result.parent).read_json('request.json')
            matched = not source['request'].get('complete_work')
        if matched:
            selected_preparations.append(observed)
            preparation_refs.add(result)
    # A source preparation consumed while authoring another candidate is not
    # itself a delivered execution. Actual runtime ancestry is still checked
    # through process edges below; a failed old run cannot certify its outputs.
    by_preparation = {owner.workspace.resolve(row['data']['prepared_result']): row for row in executions
        if row.get('data', {}).get('execution_ref') and row['data'].get('prepared_result') and
            owner.workspace.resolve(row['data']['prepared_result']) in subject_preparations}
    selected = {owner.workspace.resolve(row['data']['execution_ref']): row for row in by_preparation.values()}
    # Validate physical ancestry, but adopt declarations for current subjects.
    # A revised output may depend on an older output as source material; the
    # current producer owns that revision. Requiring every superseded ancestor
    # to be adopted by the new manuscript would make feedback impossible.
    # Source-only dependencies above still retain their unexecuted obligations.
    for identity in invocation_ids:
        store = ArtifactStore(owner.root / 'attempts' / identity)
        if store.exists('candidate_execution.json'):
            _, observed = _execution(_proof(owner),
                f'records/{identity}/candidate_execution.json', successful=True)
            if identity in subject_producers:
                selected[store.root / 'candidate_execution.json'] = observed
    for observed in selected.values():
        result = owner.workspace.resolve(observed['data']['prepared_result'])
        if result not in preparation_refs:
            preparation = ArtifactStore(result.parent.parent)
            native = preparation.read_json('native_result.json')
            _accepted(_proof(owner), native, preparation.root.name)
            selected_preparations.append(native)
            preparation_refs.add(result)
    return selected_preparations, list(selected.values())


def professional_unit(owner: Any, execution_ref: str) -> dict:
    """Consume a whole registered unit, never infer dependencies from filenames."""
    from simple_ar.tools.implementation_work import _accepted, _execution, _retained_request
    from simple_ar.tools.implementation_run import candidate_source
    proof = _proof(owner)
    saved, _ = _execution(proof, execution_ref, successful=True)
    candidate, prepared, source = candidate_source(proof, saved['prepared_result'])
    request = _retained_request(source)
    if request.work_contract != 'shared.v2' or not request.complete_work:
        raise ValueError('Whole-unit adoption requires a registered complete professional producer')
    preparation = owner.workspace.resolve(saved['prepared_result'])
    _accepted(proof, ArtifactStore(preparation.parent.parent).read_json('native_result.json'),
        preparation.parent.parent.name)
    files = {str(candidate / row['path']): row['sha256'] for row in prepared['data']['files']}
    for path, digest in saved['output_sha256'].items():
        resolved = owner.workspace.resolve_material_root(path)
        if not resolved.is_relative_to(candidate):
            raise ValueError('Professional output escapes its registered candidate unit')
        if str(resolved) in files and files[str(resolved)] != digest:
            raise ValueError('Professional output conflicts with its candidate source version')
        files[str(resolved)] = digest
    if owner.workspace.artifact_hashes(files) != files:
        raise ValueError('Professional unit sources or outputs changed')
    owner.workspace.check_artifact_producers(files)
    execution = owner.workspace.resolve(execution_ref)
    return {'contract': 'professional_delivery_unit.v1', 'execution_ref': execution_ref,
        'execution_sha256': hashlib.sha256(execution.read_bytes()).hexdigest(),
        'prepared_result': saved['prepared_result'],
        'prepared_sha256': hashlib.sha256(preparation.read_bytes()).hexdigest(),
        'source_binding_sha256': hashlib.sha256((preparation.parent / 'request.json').read_bytes()).hexdigest(),
        'candidate': str(candidate), 'files': dict(sorted(files.items()))}


def professional_unit_input_owners(owner: Any, units: list[dict]) -> dict[str, list[dict]]:
    """Resolve registered source ownership, not which bytes a process actually read.

    Commands do not enumerate sibling imports/configuration/data. Only explicit
    registered members present in the approved source binding gain this edge;
    ancillary context outside a unit is not promoted into delivery or edit scope.
    """
    from simple_ar.tools.implementation_work import _execution, _same_producer
    from simple_ar.tools.implementation_run import candidate_source
    proof, owners = _proof(owner), {}
    for unit in professional_units(owner, [], [], retained=units):
        saved, _ = _execution(proof, unit['execution_ref'], successful=True)
        candidate, prepared, _ = candidate_source(proof, saved['prepared_result'])
        outputs = owner.workspace.artifact_provenance(saved['output_sha256'])
        producers = [row['producer'] for row in outputs.values()]
        if not producers or producers[0] is None or any(
                not _same_producer(proof, producers[0], row) for row in producers):
            raise ValueError('Registered unit has no unique physical execution owner')
        from simple_ar.tools.implementation_run import execution_input_versions
        inputs = owner.workspace.resolved_versions(execution_input_versions(saved['approval']['binding']))
        for row in prepared['data']['files']:
            path = owner.workspace.resolve_material_root(str(candidate / row['path']))
            if inputs.get(path) != row['sha256'] or unit['files'].get(str(path)) != row['sha256']:
                raise ValueError('Registered source differs from its approved execution input')
            current = owners.setdefault(str(path), [])
            if not any(_same_producer(proof, producers[0], previous) for previous in current):
                current.append(producers[0])
    return owners


def professional_units(owner: Any, roots: list[str], selected: list[str], *, retained: list | None = None) -> list[dict]:
    """Select by existing delivery/producer edges; recovery uses its frozen units."""
    if retained is not None:
        for unit in retained:
            if professional_unit(owner, unit['execution_ref']) != unit:
                raise ValueError('Retained professional delivery unit changed')
        return retained
    if not selected or not roots:
        return []
    if (not ArtifactStore(owner.root).exists('session_manifest.json')
            or not any(owner.workspace.resolve(path).is_relative_to(owner.workspace.workspace) for path in selected)):
        return []  # Imported files have no registered producer in this task.
    _, executions = delivery_evidence(owner, [], {'summary': 'Inspect selected professional units', 'artifacts': selected})
    roots = [owner.workspace.resolve_material_root(path) for path in roots]
    subjects = {owner.workspace.resolve(path) for path in selected}
    units = []
    for observed in executions:
        locator = observed['data']['execution_ref']
        # Historical producers retain their original packaging behavior.
        from simple_ar.tools.implementation_run import candidate_source
        from simple_ar.tools.implementation_work import _retained_request
        candidate, _, source = candidate_source(_proof(owner), observed['data']['prepared_result'])
        request = _retained_request(source)
        if request.work_contract != 'shared.v2' or not request.complete_work:
            continue
        if not any(candidate.is_relative_to(root) or root.is_relative_to(candidate) for root in roots):
            continue
        unit = professional_unit(owner, locator)
        if subjects.intersection(owner.workspace.resolve(path) for path in unit['files']):
            units.append(unit)
    return sorted(units, key=lambda row: row['execution_ref'])


def freeze_declarations(owner: Any, artifacts: list[dict]) -> list[dict]:
    groups, seen = [], set()
    for row in artifacts:
        for producer in ([row['producer']] if row.get('producer') else []) + row.get('consumers', []):
            identity = producer['invocation_id']
            if identity in seen:
                continue
            seen.add(identity)
            record = owner.root / 'attempts' / identity / 'candidate_execution.json'
            if not record.is_file():
                continue  # Other registered process owners have different contracts.
            group = declaration_group(owner, f'records/{identity}/candidate_execution.json', producer)
            if group is not None:
                groups.append(group)
    return groups


def freeze_producer_context(owner: Any, artifacts: list[dict]) -> dict:
    """Original notes as located review leads, never a list of completion duties.

    Use the same delivery/dependency selection as the mechanical completion gate.
    This also retains source-only notes, which have no execution producer yet.
    """
    from simple_ar.tools.implementation_run import candidate_source
    from simple_ar.tools.implementation_work import _accepted, _retained_request
    preparations, executions = delivery_evidence(owner, [], {
        'summary': 'Retain producer review context', 'artifacts': [row['path'] for row in artifacts]})
    proof, groups, legacy, executed = _proof(owner), [], [], set()
    for observed in executions:
        executed.add(owner.workspace.resolve(observed['data']['prepared_result']))
        group = _producer_group(owner, observed['data']['execution_ref'])
        if group is not None and group.get('producer_completion') == 'goal_findings.v1':
            groups.append(group)
        elif group is not None:
            legacy.append(group)
    for observed in preparations:
        locator = observed['data']['prepared_result']
        if owner.workspace.resolve(locator) in executed:
            continue
        candidate, prepared, source = candidate_source(proof, locator)
        request = _retained_request(source)
        if request.producer_completion != 'goal_findings.v1' or not prepared['data'].get('unresolved'):
            continue
        record = owner.workspace.resolve(locator)
        _accepted(proof, observed, record.parent.parent.name)
        hashes = {str(candidate / row['path']): row['sha256'] for row in prepared['data']['files']}
        if owner.workspace.artifact_hashes(hashes) != hashes:
            raise ValueError('Producer note source versions changed')
        groups.append({'producer_completion': request.producer_completion,
            'prepared_result': locator, 'prepared_sha256': hashlib.sha256(record.read_bytes()).hexdigest(),
            'source_binding_sha256': hashlib.sha256((record.parent / 'request.json').read_bytes()).hexdigest(),
            'goal': request.goal, 'notes': prepared['data']['unresolved'],
            'scope': 'Prepared source only; execution and scientific correctness remain unverified.'})
    return {'producer_notes': sorted(groups, key=lambda row: (row['prepared_result'], row.get('execution_ref', ''))),
        **({'producer_declarations': sorted(legacy, key=lambda row: row['execution_ref'])} if legacy else {})}


def delivery_adoptions(owner: Any, observations: list[dict], delivery: dict | None,
                       proposed: Any = None) -> list[dict]:
    """Writing owner's proof for the canonical manuscript actually delivered."""
    from simple_ar.tools.implementation_work import _accepted
    from simple_ar.report.writing import validate_document_inspection
    if not delivery:
        return []
    from simple_ar.tools.workspace import DeliveryRequest
    try:
        delivery = DeliveryRequest.model_validate(delivery).model_dump(mode='json')
    except (ValueError, TypeError):
        return []
    report = delivery.get('report') or {}
    if report.get('status') != 'canonical' or not report.get('manuscript'):
        return []
    manuscript = owner.workspace.resolve(report['manuscript'])
    if manuscript not in {owner.workspace.resolve(path) for path in delivery.get('artifacts', [])}:
        return []
    for observed in reversed(observations):
        if observed.get('tool_name') != 'compose_document' or observed.get('status') != 'ok':
            continue
        data = observed.get('data') or {}
        if not data.get('editable_markdown') or owner.workspace.resolve(data['editable_markdown']) != manuscript:
            continue
        if (data.get('delivery_review', {}).get('producer_completion') == 'goal_findings.v1'
                and 'declaration_adoption' not in data.get('delivery_review', {})):
            return []  # This receipt has findings, never declaration-waiver authority.
        try:
            version = owner.workspace.resolve(data['version'])
            identity = version.name
            if version != owner.workspace.resolve(f'workspace/manuscripts/{identity}'):
                return []
            _accepted(_proof(owner), {key: value for key, value in observed.items()
                if key not in {'arguments', 'record', 'current_request'}}, identity)
            store = ArtifactStore(owner.root / 'tools/writing' / identity)
            result, binding = store.read_json('result.json'), store.read_json('request.json')
            if (data != result['data'] or observed.get('artifacts') != result['artifacts']
                    or data.get('unresolved') or data.get('incomplete')
                    or data.get('delivery_review', {}).get('status') != 'reviewed'
                    or not data.get('artifact_sha256')
                    or owner.workspace.artifact_hashes(data['artifact_sha256']) != data['artifact_sha256']):
                return []
            review = owner.final_review(data)
            if review.get('review_status') != 'reviewed':
                return []
            groups = binding['delivery_review'].get('producer_declarations', [])
            for group in groups:
                expected = declaration_group(owner, group['execution_ref'])
                if group != expected:
                    return []
            if not groups:
                return []  # Old reviewed receipts never gain declaration authority.
            # Reuse the existing authoring/publication/delivery-set validators;
            # this adopter does not invent a second definition of a manuscript
            # or treat unrelated attachments as having passed its inspection.
            authoring = owner.publication_source(data['editable_markdown'], data.get('report_binding'),
                observation=observed)
            from simple_ar.tools.publication import PublicationToolGateway
            publication_proposal = (proposed.arguments
                if proposed is not None and proposed.tool_name == 'export_report' else None)
            published = PublicationToolGateway.check_report_delivery(owner, observations, authoring,
                delivery['artifacts'], formats=(data.get('report_binding') or {}).get(
                    'obligation', {}).get('formats', ['markdown']),
                manuscript=report['manuscript'], proposed=publication_proposal)
            owner.check_delivery_set(authoring, delivery['artifacts'], published)
            validate_document_inspection(store, binding, data, review_result=review)
            adoption = data['delivery_review'].get('declaration_adoption', {})
            from simple_ar.report.review_evidence import declaration_adoption_contract
            contract = declaration_adoption_contract(binding['delivery_review'])
            if adoption.get('contract') != contract:
                return []
            checks = adoption.get('checks', [])
            ids = {row['id'] for group in groups for row in group['declarations']}
            if (adoption.get('producers') == groups and len(checks) == len(ids)
                    and {row['declaration_id'] for row in checks} == ids
                    and all(row['status'] in {'satisfied_by_execution', 'accepted_limitation'} for row in checks)):
                return groups
            return []
        except (OSError, ValueError, KeyError, TypeError):
            return []
    return []
