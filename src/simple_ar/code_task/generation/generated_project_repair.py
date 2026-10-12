from __future__ import annotations

"""Legacy repair application and receipts; selection/generation have shared owners.

Saved actions/content results remain readable through the historical receipts.
New requests prepare related hash-bound files in a separate candidate directory;
only this consumer can apply them to the legacy project and record its guards.
"""

import hashlib
import json
import py_compile
import re
from pathlib import Path
from typing import Any, Mapping

from simple_ar.core.artifacts import read_json, write_json
from simple_ar.core.capabilities import ArtifactStore
from simple_ar.code_task.analysis.interfaces import (
    dependency_context, find_return_contract_mismatches, project_api_contract,
)
from simple_ar.code_task.analysis.index import project_python_files
from simple_ar.code_task.analysis.entrypoints import source_suppresses_entrypoint_traceback
from simple_ar.code_task.analysis.resource_static import analyze_resource_risks
from simple_ar.code_task.editing.actions import apply_repair_actions
from simple_ar.code_task.editing.snapshots import FileSnapshotSet, create_file_snapshot_set
from simple_ar.code_task.generation.common import safe_relative_path
from simple_ar.code_task.generation.selection import select_implementation_files, bind_implementation_originals
from simple_ar.code_task.generation.writer import write_implementation_unit
from simple_ar.code_task.repair_contract import atomic_patch_set_record, normalize_repair_plan
from simple_ar.integrations.llm import LLMClient, LLMError
from simple_ar.tools.workspace import bind_project_inventory, resolve_mounted_path

_RUN_REPAIR_MAX_FILES = 8


def repair_generated_project_from_review(
    *,
    project_dir: Path,
    review_report: Mapping[str, Any],
    output_path: Path,
    code_artifacts: Mapping[str, Any] | None = None,
    architecture_plan: Mapping[str, Any] | None = None,
    result_schema: Mapping[str, Any] | None = None,
    contract: Mapping[str, Any] | None = None,
    dependency_advice: Mapping[str, Any] | None = None,
    previous_repair_context: str = "",
    client: LLMClient | None = None,
    attempt_id: str = '',
) -> dict[str, Any]:
    """Repair blocking findings through shared selection and candidate generation."""
    return _repair_generated_project(phase='review',
        diagnostics={'review_report': dict(review_report)},
        needs_repair=_review_needs_llm_repair(review_report),
        project_dir=project_dir, output_path=output_path, code_artifacts=code_artifacts or {},
        architecture_plan=architecture_plan or {}, result_schema=result_schema or {},
        contract=contract or {}, dependency_advice=dependency_advice or {},
        previous_repair_context=previous_repair_context, client=client, attempt_id=attempt_id)


def repair_generated_project_from_run_failure(
    *,
    project_dir: Path,
    failure_analysis: Mapping[str, Any],
    stderr_text: str,
    output_path: Path,
    code_artifacts: Mapping[str, Any] | None = None,
    architecture_plan: Mapping[str, Any] | None = None,
    result_schema: Mapping[str, Any] | None = None,
    contract: Mapping[str, Any] | None = None,
    dependency_advice: Mapping[str, Any] | None = None,
    previous_repair_context: str = "",
    client: LLMClient | None = None,
    attempt_id: str = '',
) -> dict[str, Any]:
    """Repair the original runtime diagnostics within the legacy finite scope."""
    return _repair_generated_project(phase='run', needs_repair=True,
        diagnostics={'failure_analysis': dict(failure_analysis), 'runtime_output': stderr_text},
        project_dir=project_dir, output_path=output_path, code_artifacts=code_artifacts or {},
        architecture_plan=architecture_plan or {}, result_schema=result_schema or {},
        contract=contract or {}, dependency_advice=dependency_advice or {},
        previous_repair_context=previous_repair_context, client=client, attempt_id=attempt_id)


def _repair_generated_project(*, phase: str, project_dir: Path, output_path: Path,
    diagnostics: dict[str, Any], needs_repair: bool, code_artifacts: Mapping[str, Any],
    architecture_plan: Mapping[str, Any], result_schema: Mapping[str, Any],
    contract: Mapping[str, Any], dependency_advice: Mapping[str, Any],
    previous_repair_context: str, client: LLMClient | None, attempt_id: str) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if attempt_id and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', attempt_id):
        raise ValueError('Invalid repair attempt identity')
    owner = ArtifactStore(output_path.parent / 'repair_attempts' / phase / (attempt_id or output_path.stem))
    binding = {'project_dir': str(project_dir.resolve()), 'phase': phase, 'attempt_id': attempt_id,
        'diagnostics': diagnostics, 'code_artifacts': dict(code_artifacts),
        'architecture_plan': dict(architecture_plan), 'result_schema': dict(result_schema),
        'contract': dict(contract), 'dependency_advice': dict(dependency_advice),
        'previous_repair_context': previous_repair_context}
    # Old summaries (including actions/content results) are completed history,
    # never a reason to ask the model to recreate that response. New rounds
    # have explicit identities supplied by the existing counter owner.
    if not owner.exists('result.json') and output_path.is_file():
        saved = read_json(output_path)
        if (isinstance(saved, dict) and saved.get('schema_version') == f'greenfield_{phase}_repair.v1'
                and not saved.get('repair_receipt')):
            if not attempt_id:
                return _historical_repair_summary(saved)
            # The existing counter owner explicitly started a new round. Keep
            # the unbound previous result, but do not let it permanently block
            # authorized future repair or masquerade as this round's result.
            owner.write_json('previous-unbound-summary.json', saved)
    if owner.exists('result.json'):
        receipt = owner.read_json('result.json')
        if receipt.get('schema_version') != 'generated_project_repair_receipt.v1':
            return _historical_repair_summary(receipt)
        try:
            if (receipt.get('completed') is not True or not isinstance(receipt.get('summary'), dict)
                    or receipt['summary'].get('schema_version') != f'greenfield_{phase}_repair.v1'):
                raise ValueError('Repair receipt lacks a completed consumer result')
            if receipt['binding'] != binding:
                raise ValueError('Repair identity inputs changed')
            if receipt['project_binding'] != _repair_project_binding(project_dir):
                raise ValueError('Project bytes/inventory changed since the completed repair')
        except (ValueError, OSError, KeyError) as exc:
            saved = receipt.get('summary') if isinstance(receipt.get('summary'), dict) else {}
            return {**saved, 'status': 'failed', 'changed_files': [], 'regenerated_files': [],
                'unresolved_errors': [f'Repair receipt rejected: {exc}; no model request or edit replayed.'],
                'historical_status': saved.get('status', 'unknown')}
        write_json(output_path, receipt['summary'])
        return receipt['summary']
    report = diagnostics['review_report'] if phase == 'review' else diagnostics['failure_analysis']
    summary: dict[str, Any] = {
        'schema_version': f'greenfield_{phase}_repair.v1', 'status': 'skipped',
        'strategy': 'llm_review_repair' if phase == 'review' else 'llm_runtime_repair',
        ('review_status' if phase == 'review' else 'failure_status'): str(report.get('status', 'unknown')),
        'changed_files': [], 'unresolved_errors': [], 'notes': [],
        'repair_receipt': str(owner.root / 'result.json'),
    }
    if phase == 'run':
        summary['ablation'] = {'use_repair_memory': bool(previous_repair_context.strip())}
    snapshot = None
    if not project_dir.is_dir():
        summary['status'] = 'failed'
        summary['unresolved_errors'].append(f'Missing generated project directory: {project_dir}')
    elif client is None:
        summary['notes'].append('Model repair unavailable; original files and diagnostics remain unchanged.')
    elif needs_repair:
        snapshot = create_file_snapshot_set(workspace_dir=project_dir,
            snapshot_root=owner.root / 'repair_snapshots', label=f'{phase}-repair')
        records = _prepare_and_apply_repair_unit(phase=phase, project_dir=project_dir.resolve(),
            owner=owner, diagnostics=diagnostics, code_artifacts=code_artifacts,
            architecture_plan=architecture_plan, result_schema=result_schema, contract=contract,
            dependency_advice=dependency_advice, previous_repair_context=previous_repair_context,
            client=client, summary=summary, snapshot=snapshot)
        if records:
            summary['regenerated_files'] = records
        if summary['unresolved_errors']:
            summary['status'] = 'failed'
        elif summary['changed_files']:
            summary['status'] = 'patched'
            summary['notes'].append('Applied related candidate edits; rerun review/benchmark before accepting repair.')
    if snapshot is not None:
        _attach_snapshot_summary(summary, snapshot)
    owner.write_json('result.json', {'schema_version': 'generated_project_repair_receipt.v1',
        'completed': True, 'binding': binding,
        'project_binding': _repair_project_binding(project_dir), 'summary': summary})
    write_json(output_path, summary)
    return summary


def _repair_project_binding(project_dir: Path) -> dict[str, Any]:
    project = project_dir.resolve()
    if not project.is_dir():
        return {'project_path': str(project), 'missing': True}
    return bind_project_inventory(project,
        resolve=lambda value: resolve_mounted_path({'project': project}, value))


def _historical_repair_summary(saved: Mapping[str, Any]) -> dict[str, Any]:
    return {**saved, 'status': 'skipped', 'historical_status': saved.get('status', 'unknown'),
        'historical_result': dict(saved),
        'changed_files': [], 'regenerated_files': [], 'current_project_verified': False,
        'notes': [*saved.get('notes', []),
            'Unbound legacy repair result retained as history only; current project repair is not certified.']}


def _prepare_and_apply_repair_unit(*, phase: str, project_dir: Path, owner: ArtifactStore,
    diagnostics: dict[str, Any], code_artifacts: Mapping[str, Any],
    architecture_plan: Mapping[str, Any], result_schema: Mapping[str, Any],
    contract: Mapping[str, Any], dependency_advice: Mapping[str, Any],
    previous_repair_context: str, client: LLMClient, summary: dict[str, Any],
    snapshot: FileSnapshotSet) -> list[dict[str, Any]]:
    # A read-only mounted view of the existing project, not a new tool runtime.
    resolve = lambda value: resolve_mounted_path({'project': project_dir}, value)
    bind = lambda: bind_project_inventory(project_dir, resolve=resolve)
    goal = ('Repair the blocking review findings in this generated project.' if phase == 'review'
        else 'Repair this generated project from the actual failed benchmark diagnostics.')
    goal += (' Preserve public API signatures, measured metrics, architecture and dependency obligations. '
        'Fix producer/consumer causes; preserve traceback visibility, required artifact paths and '
        'the legacy main.py metric-printing contract. Do not invent successful results or suppress failures. '
        'Use prior repair history to avoid repeating ineffective fixes. Execution remains with the caller.')
    binding = {'phase': phase, 'goal': goal, 'diagnostics': diagnostics,
        'code_artifacts': dict(code_artifacts), 'architecture_plan': dict(architecture_plan),
        'result_schema': dict(result_schema), 'contract': dict(contract),
        'dependency_advice': dict(dependency_advice), 'previous_repair_context': previous_repair_context}
    # Complete diagnostic/obligation values are supplied once per shared request.
    # Project snippets and unread coverage come from the existing selector.
    windows = [{'role': 'legacy_repair_diagnostics_and_obligations', **binding},
        {'role': 'derived_static_project_observations',
            'project_api': project_api_contract(project_dir),
            'return_contract_mismatches': find_return_contract_mismatches(project_dir),
            'resource_static': analyze_resource_risks(project_dir)}]
    inventory = bind()
    existing = {Path(path).relative_to(project_dir).as_posix()
        for path in inventory['file_hashes'] if Path(path).suffix == '.py'}
    architecture_files = architecture_plan.get('files')
    specs = {path: dict(row) for row in (architecture_files if isinstance(architecture_files, list) else [])
        if isinstance(row, Mapping) and (path := safe_relative_path(str(row.get('path', ''))))}
    allowed = existing if phase == 'run' else None
    if phase == 'run' and not allowed:
        summary['unresolved_errors'].append('No Python files within the legacy repair scope.')
        return []
    records: list[dict[str, Any]] = []
    plan: dict[str, Any] = {}
    try:
        if owner.exists('generation-input.json'):
            state = 'candidate saved, application incomplete' if owner.exists('candidate.json') else 'generation incomplete or unknown'
            raise ValueError(f'Interrupted repair: {state}; inspect {owner.root}; no request or edit replayed')
        selection, _ = select_implementation_files(goal=goal, request_binding=binding,
            project=project_dir, owner=owner, windows=windows, bind_inventory=bind,
            resolve_source=resolve, validate_destination=lambda path: _repair_destination(project_dir, path),
            client_factory=lambda: client, max_files=_RUN_REPAIR_MAX_FILES if phase == 'run' else 12,
            allowed_paths=allowed, allow_new_files=phase == 'review')
        files = selection['files']
        plan = normalize_repair_plan({'target_files': [
            {'path': row['path'], 'rationale': row['purpose']} for row in files],
            'repair_strategy': 'Shared related-file implementation candidate; legacy guarded application.'},
            failure_analysis={**diagnostics.get('failure_analysis', diagnostics.get('review_report', {})),
                'summary': diagnostics.get('runtime_output', '') or str(diagnostics.get('review_report', {}))},
            contract=contract)
        if phase == 'run':
            write_json(owner.root / 'run_repair_plan.json', plan)
            write_json(owner.root.parents[2] / 'run_repair_plan.json', plan)
            summary['repair_plan'] = str(owner.root / 'run_repair_plan.json')
        originals = bind_implementation_originals(files, resolve_source=resolve)
        generation_input = {'selection': selection, 'original_files': originals, 'binding': binding}
        owner.write_json('generation-input.json', generation_input)
        # Full architecture declarations already occur in windows; add only
        # the observed dependency APIs, not a second per-file declaration.
        obligations = [{'path': row['path'],
            'actual_dependency_apis': dependency_context(project_dir,
                specs.get(row['path'], row), max_source_chars=5000)} for row in files]
        candidate = owner.root / 'candidate'
        result = write_implementation_unit(project_dir=candidate, goal=goal,
            files=files, source_windows=[*windows,
                {'role': 'owner_project_context_not_execution_authority',
                    'declared_project': selection['declared_project'], 'coverage': selection['coverage'],
                    'snapshots': [row for row in selection['source_context'] if row['path'] not in originals]},
                {'role': 'selected_actual_dependency_apis', 'files': obligations}],
            original_files=originals, client=client, artifact_dir=owner.root / 'generation',
            retry_attempts=1)
        owner.write_json('candidate.json', result)
        if phase == 'run':
            # The shared generator's diagnosis/remaining gaps enrich the same
            # legacy plan record; no second diagnosis request or state owner.
            plan = normalize_repair_plan({**plan, 'root_cause': result['summary'],
                'risks': result['unresolved']}, failure_analysis=diagnostics['failure_analysis'], contract=contract)
            owner.write_json('run_repair_plan.json', plan)
            write_json(owner.root.parents[2] / 'run_repair_plan.json', plan)
        if result['unresolved']:
            raise ValueError('Candidate reports unresolved repair gaps: ' + '; '.join(result['unresolved']))
        if bind() != {key: selection['input'][key]
                for key in ('project_path', 'file_hashes', 'inventory_scope')}:
            raise ValueError('Project changed during repair generation; candidate was not applied')
        # Verify all candidate bytes before the first write to the old project.
        for row in result['files']:
            if hashlib.sha256((candidate / row['path']).read_bytes()).hexdigest() != row['sha256']:
                raise ValueError('Repair candidate changed before application')
        for row in files:
            path = row['path']
            text = (candidate / path).read_bytes().decode('utf-8')
            original = originals.get(path)
            if original and text == original['text']:
                continue
            captured = snapshot.capture(path)
            if (captured is None or bool(captured.get('existed')) != bool(original)
                    or (original and captured.get('sha256') != original['sha256'])):
                raise ValueError('Repair source changed before snapshot/application')
            applied = _apply_candidate_file(project_dir=project_dir, rel_path=path,
                content=text, existed=bool(original), candidate_summary=result['summary'],
                changed=summary['changed_files'], notes=summary['notes'],
                unresolved=summary['unresolved_errors'], mode_prefix=f'llm_{phase}_repair',
            )
            if applied is not None:
                records.append(applied)
            if summary['unresolved_errors']:
                break
        if phase == 'run' and not summary['unresolved_errors']:
            summary['unresolved_errors'].extend(_compile_project(project_dir))
    except (LLMError, ValueError, SyntaxError) as exc:
        summary['unresolved_errors'].append(f'{phase} repair unavailable: {exc}')
    except Exception as exc:
        # Internal errors propagate, but never leave half an associated unit.
        restore_errors = _rollback_repair_unit(snapshot)
        owner.write_json('application-error.json', {'error_type': type(exc).__name__,
            'error': str(exc), 'restore_errors': restore_errors, 'snapshot': snapshot.artifact_record()})
        for error in restore_errors:
            exc.add_note(error)
        summary['changed_files'].clear()
        raise
    if summary['unresolved_errors']:
        restore_errors = _rollback_repair_unit(snapshot)
        summary['unresolved_errors'].extend(restore_errors)
        summary['changed_files'].clear()
        records.clear()
        summary['notes'].append('Rejected related repair unit; ' +
            ('snapshot restoration incomplete; inspect retained snapshot.' if restore_errors else 'captured files restored.'))
    if phase == 'run':
        atomic = atomic_patch_set_record(repair_plan=plan, applied_records=records,
            snapshot=snapshot.artifact_record() if snapshot.captured_count else {},
            post_validation={'unresolved_errors': summary['unresolved_errors']})
        if summary['unresolved_errors']:
            atomic['status'] = 'failed'
        owner.write_json('atomic_patch_set.json', atomic)
        write_json(owner.root.parents[2] / 'atomic_patch_set.json', atomic)
        summary['atomic_patch_set'] = str(owner.root / 'atomic_patch_set.json')
    return records


def _rollback_repair_unit(snapshot: FileSnapshotSet) -> list[str]:
    try:
        events = snapshot.restore()
        errors = [f"Snapshot restore failed for {row['path']}: {row.get('reason', row['status'])}"
            for row in events if row['status'] not in {'restored_file', 'removed_created_file', 'already_missing'}]
        for row in read_json(snapshot.manifest_path)['files']:
            path = resolve_mounted_path({'project': snapshot.workspace_dir},
                str(snapshot.workspace_dir / row['path']))
            if row.get('existed'):
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row.get('sha256'):
                    errors.append(f"Snapshot restore byte check failed for {row['path']}")
            elif path.exists():
                errors.append(f"Snapshot restore left created path {row['path']}")
        return errors
    except Exception as exc:
        return [f'Snapshot restoration raised {type(exc).__name__}: {exc}; inspect {snapshot.manifest_path}']


def _repair_destination(project_dir: Path, path: str) -> Path:
    if not path.endswith('.py'):
        raise ValueError('Legacy repair scope permits Python files only')
    return resolve_mounted_path({'project': project_dir}, str(project_dir / path))


def _apply_candidate_file(
    *,
    project_dir: Path,
    rel_path: str,
    content: str,
    existed: bool,
    candidate_summary: str,
    changed: list[str],
    notes: list[str],
    unresolved: list[str],
    mode_prefix: str,
) -> dict[str, Any] | None:
    """Apply decoded candidate text through the legacy deterministic actions.

    Selection/original binding and response decoding have already completed.
    This consumer never reinterprets model responses. Existing action write
    semantics remain with apply_repair_actions; the enclosing unit owns rollback.
    """

    target = project_dir / rel_path
    actions = [{"action": "rewrite_file" if existed else "add_file",
        "path": rel_path, "content": content, "rationale": candidate_summary}]
    applied = apply_repair_actions(project_dir, actions, allowed_paths={rel_path})
    error = ""
    if applied["rejected_actions"]:
        error = "structured repair actions were rejected: " + json.dumps(
            applied["rejected_actions"], ensure_ascii=False
        )[:1000]
    elif applied["status"] != "patched":
        notes.append(f"Skipped {rel_path}; repair actions made no changes.")
        return None
    else:
        error = (
            (_compile_error(target) if target.suffix == ".py" and target.is_file() else "")
            or _post_write_static_guard(target=target, rel_path=rel_path)
            or ('public_api_signature_would_change' if _public_api_contract_breaks(
                applied['applied_actions'][0]['before_public_api'],
                applied['applied_actions'][0]['after_public_api']) else '')
        )
    if error:
        # Stop application here. The enclosing unit restores all captured
        # paths once and verifies bytes, retaining any restoration failures.
        unresolved.append(f"{rel_path}: repair rejected: {error}")
        return None
    if rel_path not in changed:
        changed.append(rel_path)
    notes.append(f"Applied repair actions to {rel_path}.")
    return {
        "path": rel_path,
        "mode": f"{mode_prefix}_actions",
        "line_count": _file_line_count(target),
        "summary": candidate_summary[:500],
        "public_api": applied['applied_actions'][0]['after_public_api'],
        "edit_application": applied,
    }


def _attach_snapshot_summary(summary: dict[str, Any], snapshot: FileSnapshotSet) -> None:
    if snapshot.captured_count or snapshot.restored_count:
        snapshot.write_manifest()
        summary["snapshot"] = snapshot.artifact_record()


def _public_api_contract_breaks(before_api: list[str], after_api: list[str]) -> bool:
    before_map = _public_api_map(before_api)
    after_map = _public_api_map(after_api)
    for name, signature in before_map.items():
        if name not in after_map:
            return True
        if after_map[name] != signature:
            return True
    return False


def _public_api_map(rows: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in rows:
        name = _api_name_from_signature(str(item))
        if name and name not in result:
            result[name] = str(item)
    return result


def _post_write_static_guard(*, target: Path, rel_path: str) -> str:
    if target.suffix != ".py" or not target.is_file():
        return ""
    try:
        source = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"could_not_read_repaired_file:{exc}"
    suppressed = source_suppresses_entrypoint_traceback(source, path=rel_path)
    if suppressed:
        return (
            f"{suppressed}; generated entrypoints must preserve the original traceback "
            "or re-raise broad exceptions so runtime repair can localize the true failing file."
        )
    return ""


def _api_name_from_signature(value: str) -> str:
    text = value.strip()
    if text.startswith("class "):
        text = text.split(" ", 1)[1]
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", text)
    return match.group(1) if match else ""


def _file_line_count(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        return max(1, len(path.read_text(encoding="utf-8", errors="replace").splitlines()))
    except OSError:
        return 0


def _review_needs_llm_repair(review_report: Mapping[str, Any]) -> bool:
    """Use the review findings; this boundary has not silently repaired them."""

    return any(
        (str(item.get("severity", "blocking")).strip() or "blocking") == "blocking"
        for item in _review_findings(review_report)
    )


def _review_findings(review_report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    findings = review_report.get("findings")
    if isinstance(findings, list):
        return [item for item in findings if isinstance(item, Mapping)]
    quality = review_report.get("quality")
    if isinstance(quality, Mapping):
        nested = quality.get("findings")
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, Mapping)]
    return []


def _compile_error(path: Path) -> str:
    try:
        py_compile.compile(str(path), doraise=True)
    except py_compile.PyCompileError as exc:
        return exc.msg
    return ""


def _compile_project(project_dir: Path) -> list[str]:
    errors: list[str] = []
    for path in project_python_files(project_dir):
        error = _compile_error(path)
        if error:
            errors.append(f"{path.relative_to(project_dir).as_posix()}: {error}")
    return errors
