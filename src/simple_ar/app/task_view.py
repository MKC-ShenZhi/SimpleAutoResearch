"""Read saved task facts without assembling or advancing an application.

The caller owns record admission and transport privacy. This module owns the
task projection, including existing approval owners and receipt interpretation;
it never resolves a model, dispatches work, writes records or repairs a task.
"""
from __future__ import annotations

from copy import deepcopy
from itertools import islice
import json
from pathlib import Path
import re
from typing import Callable
from types import SimpleNamespace

from simple_ar.core.budget import BudgetLedger
from simple_ar.core.feedback import FeedbackSubmissions
from simple_ar.tools.implementation import ImplementationToolGateway
from simple_ar.tools.sources import SourceToolGateway
from simple_ar.tools.workspace import WorkspaceToolGateway, validate_workspace_arguments, workspace_history_identity


RecordReader = Callable[[str], dict]
_READ_ERRORS = (OSError, ValueError, KeyError, TypeError)
_APPROVAL_TOKEN = re.compile(r'Process requires explicit approval; token=([a-f0-9]{64})')


def saved_work_connection(binding: dict, purpose: str) -> dict:
    """Resolve the saved professional route or original task settings."""
    route = binding.get(f'{purpose}_route') if purpose in {'code', 'writing'} else None
    return route or {'connection': binding.get('settings') if purpose != 'code' else None}


def history_observation(call: dict, result: dict, record: str, *, navigation: bool = False) -> dict:
    """One receipt-derived index for both current context and history pages."""
    try:
        arguments = json.loads(call['function']['arguments'])
    except (ValueError, TypeError):
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    data = result.get('data') or {}
    name = call['function']['name']
    argument_keys = (arguments.keys() if navigation else
        ('path', 'document_record', 'query', 'offset_bytes', 'start_line', 'max_lines',
         'argv', 'cwd', 'timeout_sec', 'url', 'paper_id', 'search_record'))
    indexed_arguments = {key: arguments[key] for key in argument_keys
        if key in arguments and (not navigation or key not in {'content', 'text', 'replacements'})
        and len(json.dumps(arguments[key], ensure_ascii=False)) <= 1024}
    observed = {key: value for key, value in data.items()
        if key != 'text' and not (navigation and key == 'streams')
        and len(json.dumps(value, ensure_ascii=False)) <= 1024
        and not (name == 'focus_work' and key == 'inputs')
        and not (key in indexed_arguments and value == indexed_arguments[key])
        and not (key == 'absolute_path' and value == indexed_arguments.get('path'))}
    if result.get('status') == 'ok' and not result.get('error'):
        observed.update(SourceToolGateway.history_identity(name, data)
            or workspace_history_identity(name, data))
    outcome = {'tool': name,
        'record': 'records/' + record.removeprefix('attempts/') if record.startswith('attempts/') else None,
        'inline_result': result if not record.startswith('attempts/') else None,
        'status': result.get('status'), 'error': result.get('error', ''),
        'arguments': indexed_arguments, 'observed': observed,
        'artifacts': result.get('artifacts', []), 'summary': result.get('summary', '')}
    if navigation:
        # Explicit recovery for omitted payloads; no new persisted memory.
        outcome['omitted_fields'] = {
            'arguments': [key for key in arguments if key not in indexed_arguments],
            'data': [key for key in data if key not in observed],
        }
        for key in ('error', 'summary'):
            value = outcome[key]
            if isinstance(value, str) and len(value) > 1024:
                outcome[key] = {'preview': value[:1024],
                    'omitted_characters': len(value) - 1024,
                    'record': outcome['record'], 'field': key}
    return {key: value for key, value in outcome.items()
        if key in {'arguments', 'observed'}
        or (value is not None and value != '' and value != [] and value != {})}


def saved_model_recipients(binding: dict) -> dict:
    """Project pinned recipients without consulting live settings or credentials."""
    settings = binding.get('settings') or {}
    connections = {'task': settings}
    connections.update({purpose: saved_work_connection(binding, purpose).get('connection')
                        for purpose in ('code', 'writing')})
    return {purpose: {key: value.get(key) for key in ('base_url', 'model', 'api', 'api_mode') if key in value}
            if value else None for purpose, value in connections.items()}


def _execution_projection(root: Path, manifest: dict, state: dict, ledger: BudgetLedger,
                          active: dict, authorize_execution: Callable[[], None] | None,
                          native_recipe: str | None) -> dict:
    """Recompute supported bindings through their existing read-only owners."""
    if authorize_execution is not None:
        authorize_execution()
    if any(not (root / directory).is_dir() for directory in ('workspace', 'evidence')):
        raise ValueError('Prepared task directory unavailable')
    config = state['config']
    call = active['call']
    name = call['function']['name']
    if name == 'execute_implementation' and (
            native_recipe is None or manifest.get('transition_recipe') != native_recipe
            or config.get('code_task') is not None):
        raise ValueError('Prepared approval is not available for this task owner')
    workspace = WorkspaceToolGateway(root, {
        **{alias: Path(path) for alias, path in config['sources'].items()},
        'evidence': root / 'evidence', 'records': root / 'attempts'}, ledger,
        project_python=Path(config['project_python']) if config.get('project_python') else None,
        create_workspace=False)
    owner = (ImplementationToolGateway(root, workspace, ledger, config['model'], manifest['session_id'])
             if name == 'execute_implementation' else None)
    projection = workspace.execution_approval(call, pending=state['pending'], owner=owner)
    binding = projection['binding']
    timeout = (binding['plan']['timeout_sec'] if owner is not None else
               validate_workspace_arguments('run_process', json.loads(call['function']['arguments'])).timeout_sec)
    return {'token': projection['token'], 'execution': {
        'argv': binding['argv'], 'cwd': binding['cwd'], 'timeout_sec': timeout}}


def _approval(root: Path, manifest: dict, state: dict, ledger: BudgetLedger,
              read_record: RecordReader, authorize_execution: Callable[[], None] | None,
              native_recipe: str | None) -> tuple[dict | None, str | None]:
    pending = state.get('pending')
    token = _APPROVAL_TOKEN.search(manifest.get('status_reason', ''))
    if not token or not pending or pending.get('kind') != 'tool':
        return None, None
    # Nested inspection actions own their attempts and historical binding shape.
    active = pending.get('action') or pending
    call = active.get('call') if isinstance(active, dict) else None
    function = call.get('function') if isinstance(call, dict) else None
    if (not isinstance(active, dict) or active.get('kind') != 'tool'
            or not isinstance(function, dict) or not isinstance(function.get('name'), str)
            or not isinstance(function.get('arguments'), str) or not active.get('attempt') or active.get('result')):
        return None, 'Pending command unavailable; resume to inspect the current proposal.'
    approval = {'token': token[1], 'call': call, 'attempt': active['attempt']}
    try:
        if function['name'] in {'run_process', 'execute_implementation'}:
            projection = _execution_projection(root, manifest, state, ledger, active, authorize_execution, native_recipe)
            if projection['token'] != token[1]:
                raise ValueError('Execution changed since approval was requested')
            approval['execution'] = projection['execution']
        if read_record('actions.json') != state or read_record('session_manifest.json') != manifest:
            raise ValueError('Pending task changed during approval inspection')
    except (*_READ_ERRORS, RuntimeError):
        return None, 'Command unavailable or changed; resume to inspect the current proposal before approval.'
    return approval, None


def _activity(state: dict, read_record: RecordReader, limit: int) -> tuple[dict | None, list[dict]]:
    plan, activity = None, []
    recent = islice(((turn, item) for turn in reversed(state['turns'])
                     for item in reversed(turn['tools'])), limit)
    for turn, item in recent:
        ref = item['result']
        row = dict(record=ref, call_id=item['id'], request_count=turn.get('request_count'),
                   attempt=Path(ref).parent.name if ref.startswith('attempts/') else None)
        try:
            result = read_record(ref)
            try:
                reply = read_record(turn['reply'])
                call = next(c for c in reply.get('tool_calls', []) if c['id'] == item['id'])
            except (*_READ_ERRORS, StopIteration):
                call = {'function': {'name': result['tool_name'], 'arguments': '{}'}}
                row['action_unavailable'] = True
            row.update(history_observation(call, result, ref))
            row['record'] = ref
            if (plan is None and result.get('tool_name') == 'update_work_plan'
                    and result.get('status') == 'ok'
                    and result.get('data', {}).get('request_count') == len(state['requests'])):
                plan = result['data']
        except (*_READ_ERRORS, StopIteration):
            row.update(status='unavailable', error='Original action/receipt unavailable; no outcome inferred')
        activity.append(row)
    return plan, activity


def _pending_receipt(pending: dict | None, read_record: RecordReader) -> dict | None:
    if not pending:
        return None
    view = deepcopy(pending)
    ref = f"attempts/{pending['attempt']}/capability_result.json"
    try:
        result = read_record(ref)
        view['receipt'] = {key: result.get(key) for key in ('status', 'diagnostics')}
        view['receipt']['record'] = ref
    except _READ_ERRORS:
        view['receipt'] = None
    return view


def feedback_receipts(state: dict, read_record: RecordReader) -> list[dict]:
    """Admit the saved journal once, then delegate adoption semantics to its owner."""
    try:
        journal = read_record(FeedbackSubmissions.path)
    except FileNotFoundError:
        return []
    # This is an immutable read adapter for an already admitted record, not a
    # second store or journal. Permission/format errors must not look like absence.
    records = SimpleNamespace(exists=lambda path: path == FeedbackSubmissions.path,
        read_json=lambda path: journal)
    return FeedbackSubmissions(records).receipts(state)


def read_task_view(root: Path, *, read_record: RecordReader, ledger: BudgetLedger,
                   authorize_execution: Callable[[], None] | None = None,
                   native_recipe: str | None = None,
                   activity_limit: int = 24) -> dict:
    """Public saved-task view for CLI, GUI and result_snapshot consumers.

    ``read_record`` reads task-relative JSON under the caller's admission rules;
    ``ledger`` is the existing ledger, used only for snapshots. The optional
    callback admits execution inputs before approval owners read them (GUI launch
    grants, for example). It grants no execution. No application load is needed.
    Pass the caller's existing native recipe for prepared implementation approval;
    omission withholds that approval rather than guessing an executable owner.

    Recent activity is bounded and receipt-derived, not a quality certificate.
    Concurrent writes can make the display stale; approval is withheld if its
    records change during inspection, and execution must still recheck binding.
    Returned objects are detached from supplied records. HTTP redaction, local
    worker liveness and filesystem grants remain the adapter's responsibility.
    """
    if type(activity_limit) is not int or not 0 <= activity_limit <= 256:
        raise ValueError('activity_limit must be between 0 and 256')
    root = Path(root)
    manifest = read_record('session_manifest.json')
    state = read_record('actions.json')
    approval, unavailable = _approval(root, manifest, state, ledger, read_record, authorize_execution, native_recipe)
    plan, activity = _activity(state, read_record, activity_limit)
    config = state['config']
    result = dict(
        manifest={key: manifest.get(key) for key in ('session_id', 'topic', 'status', 'status_reason',
            'current_attempt', 'revision', 'budget', 'updated_at')},
        requests=state['requests'], plan=plan, pending=_pending_receipt(state.get('pending'), read_record),
        feedback_submissions=feedback_receipts(state, read_record),
        approval=approval, approval_unavailable=unavailable,
        activity=activity, activity_total=sum(len(turn['tools']) for turn in state['turns']),
        queued_actions=sum(len(turn.get('remaining', [])) for turn in state['turns']),
        model=config.get('model'), saved_connections={
            **saved_model_recipients(config.get('model_connection') or {}),
            **{purpose: saved_model_recipients(config[field])['task']
               for purpose, field in (('vision', 'vision_connection'), ('feasibility', 'feasibility_connection'))
               if config.get(field)}},
        catalog_hints=config.get('model_catalogs', {}), access={key: config.get(key) for key in
            ('allow_network', 'allow_execution', 'allow_vision')},
        sources=list(config.get('sources', {})),
        resources=ledger.snapshot() | {'entries': [
            {key: entry.to_dict()[key] for key in ('reservation_id', 'status', 'reserved', 'actual',
                'purpose', 'attempt_id', 'actual_source', 'reason')} for entry in ledger.entries]})
    return deepcopy(result)
