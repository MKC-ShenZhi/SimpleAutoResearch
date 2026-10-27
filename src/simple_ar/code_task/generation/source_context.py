"""Pure projection of source-owner receipts into the common working selector.

The caller validates current source/draft bytes once. Original native records
stay intact; completed blocks may leave presentation, never their read proofs.
"""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Callable

from simple_ar.core.working_context import select_working_context


def source_messages(*, system: str, context: dict, blocks: list[dict],
                    record_root: str, draft_paths: dict[str, str], versions: dict[str, str],
                    tools: list[dict], input_target: int,
                    estimate_tokens: Callable[[str], int]) -> tuple[list[dict], dict]:
    """Select exact observations, not generated summaries or truncated native pairs."""
    base = deepcopy(context)
    originals = base.pop('source_windows', [])
    # Requests, permissions, original reviews/notes, execution diagnostics and
    # actual validation failures remain mandatory. File bodies alone compete.
    base['source_windows'] = [row for row in originals if 'request' not in row]
    observations, native, completed = [], [], {}
    source_record = record_root + '/request.json'
    for row in reversed(originals):
        if 'request' not in row or not isinstance(row.get('text'), str):
            continue
        request = row['request']
        observations.append({**deepcopy(row), 'record': source_record,
            'path': row.get('absolute_path') or request['path'],
            'offset_bytes': row.get('offset_bytes', request.get('offset_bytes', 0)),
            'kind': 'source_read',
            'evidence_role': 'Actual authorized source read; not independently certified evidence.'})
    projected = 0
    for block in blocks:
        pair = block['messages']
        assistant, tool = pair
        call = assistant['tool_calls'][0]['function']
        name, arguments = call['name'], json.loads(call['arguments'])
        result = json.loads(tool['content'])
        record = record_root + '/decision-steps/' + str(block['step'])
        data = result.get('data') or {}
        relative = arguments.get('path')
        draft_path = draft_paths.get(relative)
        byte_operation = (name in {'stage_candidate_file', 'write_candidate_file',
            'edit_candidate_file', 'read_candidate_file'} and result.get('status') == 'ok'
            and not result.get('error') and draft_path is not None
            and data.get('absolute_path') == draft_path and isinstance(data.get('sha256'), str))
        source_operation = (name in {'read_source_windows', 'focus_source_observations'}
            and result.get('status') == 'observed')
        diagnosis = block.get('diagnosis')
        diagnosed = diagnosis is not None and diagnosis in base.get('validation_observations', [])
        if not (byte_operation or source_operation or diagnosed):
            native.append(deepcopy(pair))  # Preserve the entire block, including reasoning.
            continue
        projected += 1
        native.append([])  # Complete block archived; no synthetic assistant/tool conversation.
        if not byte_operation:
            continue
        if name == 'read_candidate_file':
            observations.append({**deepcopy(data), 'request': deepcopy(arguments),
                'record': record + '/candidate-operation.json', 'field': 'result.data.text',
                'path': draft_path, 'offset_bytes': data.get('offset_bytes', arguments.get('offset_bytes', 0)),
                'kind': 'author_draft_read', 'evidence_role': 'Authored candidate bytes; never original source evidence.'})
        else:
            completed[relative] = {'record': record + '/candidate-operation.json', 'action': name,
                'written_sha256': data['sha256'], 'current_sha256': versions.get(draft_path)}
    # Prefer the latest authored ranges; stale ranges remain referenced but the
    # common selector will not present them as current bytes after an edit.
    observations = ([row for row in observations if row['kind'] == 'source_read']
        + list(reversed([row for row in observations if row['kind'] == 'author_draft_read'])))
    base['candidate_draft']['completed_operations'] = completed
    base['source_context'] = {'contract': 'working.v1', 'raw_decisions': record_root + '/decision-steps',
        'projected_completed_blocks': projected,
        'scope': 'Successful byte receipts locate current candidate artifacts; staging is not source inspection. '
            'Capacity-omitted active source bodies remain in request.json; focus_source_observations '
            'can retain their existing observation_ref. read_source_windows acquires necessary new '
            'ranges, not repeats of the same already-active window. '
            'Omitted authored code can be read through read_candidate_file. Read coverage/admission uses '
            'all original byte-owner records, independently of presentation.'}
    messages, selection = select_working_context(user_requests=[base['goal']], system_text=system,
        base_context=base, turn_blocks=native, observations=observations,
        schema_tokens=estimate_tokens(json.dumps(tools, ensure_ascii=False)),
        context_token_target=input_target, estimate_tokens=estimate_tokens,
        selected_records=list(dict.fromkeys(row['record'] for row in observations)),
        current_artifacts=list(draft_paths.values()), version_resolver=versions.get,
        material_policy='referenced',
        history_access='focus_source_observations / read_source_windows / read_candidate_file')
    if selection['context_exceeds_input_target']:
        raise ValueError('Required source work context exceeds the shared input target; no request sent')
    return messages, selection
