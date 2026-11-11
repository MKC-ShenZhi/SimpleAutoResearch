"""Pure selection of recorded working material; no I/O, state or summarizer."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from itertools import zip_longest
import json
import math
import re
from typing import Any, Literal


CITATION_HANDOFF_POLICY = 'citations.v1'
TURN_SUPPLY_POLICY = 'turns.v1'
RECEIPT_SUPPLY_POLICIES = frozenset({'receipts.v1', TURN_SUPPLY_POLICY})


def material_window_views(observation: Mapping[str, Any], supplied: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Show only unsupplied reversible text of one original same-path version.

    These are presentation slices, not new reads. Original receipt/range fields
    stay intact; record_text_slice and supplied byte bounds describe the actual
    displayed text. Equal hashes at another path never establish a relationship.
    """
    row = deepcopy(dict(observation))
    path = row.get('absolute_path') or row.get('path')
    start, end, text = row.get('offset_bytes'), row.get('next_offset_bytes'), row.get('text')
    if (row.get('kind') != 'source_text' or row.get('role') != 'historical_observation'
            or not path or not row.get('sha256') or type(start) is not int or type(end) is not int
            or not isinstance(text, str) or '\ufffd' in text or end <= start):
        return [row]
    raw = text.encode('utf-8')
    if len(raw) != end - start:
        return [row]  # No reversible byte map: keep its original observation.
    ranges = [(start, end)]
    for other in supplied:
        if ((other.get('absolute_path') or other.get('path')) != path
                or other.get('sha256') != row['sha256'] or other.get('kind') != 'source_text'
                or other.get('role') != 'historical_observation'):
            continue
        left = other.get('supplied_offset_bytes', other.get('offset_bytes'))
        right = other.get('supplied_next_offset_bytes', other.get('next_offset_bytes'))
        body = other.get('text')
        if (type(left) is not int or type(right) is not int or not isinstance(body, str)
                or '\ufffd' in body or len(body.encode('utf-8')) != right - left):
            continue
        lo, hi = max(start, left), min(end, right)
        if lo >= hi or raw[lo-start:hi-start] != body.encode('utf-8')[lo-left:hi-left]:
            continue
        ranges = [(a, b) for first, last in ranges for a, b in
            ((first, min(last, left)), (max(first, right), last)) if a < b]
    if ranges == [(start, end)]:
        return [row]
    views = []
    for left, right in ranges:
        try:
            prefix, body = raw[:left-start].decode('utf-8'), raw[left-start:right-start].decode('utf-8')
        except UnicodeDecodeError:
            return [row]
        views.append({**row, 'text': body,
            'record_text_slice': {'character_start': len(prefix), 'character_end': len(prefix) + len(body),
                'total_characters': len(text), 'truncated': True},
            'supplied_offset_bytes': left, 'supplied_next_offset_bytes': right,
            'coverage': 'Presentation slice of this original read; other displayed same-path version windows '
                'carry the overlapping text. Original receipt and read coverage are unchanged.'})
    return views


def feedback_observations(context: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project related feedback as optional guidance, never source reads.

    Requests and working notes already have their native presentation. The
    original result record remains readable even if its whole body cannot fit.
    Subject bindings describe relevance/currentness, not new certification.
    """
    reviews = [*context.get('review_observations', ()), *context.get('professional_findings', ())]
    reviews.sort(key=lambda row: not any(
        subject.get('current_matches_review') for subject in row.get('subjects', ())))
    projected = []
    for row in reviews:
        source = (row['result'].get('data', {}).get('source_observations') or {}).get('record')
        record = source if isinstance(source, str) and source.startswith('records/') else row['record']
        role = ('Retained related feedback' if row.get('opinion_available', True)
                else 'Unavailable feedback diagnostic')
        projected.append({'record': record, 'kind': 'operation_result', 'related_feedback': True,
            'text': json.dumps(row['result'], ensure_ascii=False, separators=(',', ':')),
            'subjects': deepcopy(row['subjects']),
            'evidence_role': role + '; guidance only, not source evidence, current certification or permission.'})
    return projected


def history_view(turns: list[dict[str, Any]]) -> Any:
    # Lossless presentation of receipt-derived indexes, never saved memory.
    columns, rows = [], []
    for turn in turns:
        observations = []
        for observation in turn['observations']:
            keys = list(observation)
            if keys not in columns:
                columns.append(keys)
            observations.append([columns.index(keys), list(observation.values())])
        rows.append([turn['reply'], turn['request_count'], observations])
    table = {'columns': ['reply', 'request_count', 'observations'],
        'observation_columns': columns, 'rows': rows}
    return table if len(json.dumps(table, ensure_ascii=False)) < len(
        json.dumps(turns, ensure_ascii=False)) else turns


def current_material_paths(*, identities: Sequence[Mapping[str, Any]] = (),
                           request_count: int, selected_paths: Sequence[str] = ()) -> set[str]:
    """Qualify current production and owner-selected material, without reading bytes.

    A delivery inventory is discovery, not relevance. Owners resolve selections
    under their existing permissions; the selector still checks read versions.
    """
    paths = set(selected_paths)
    for row in identities:
        if (row.get('presence') == 'present' and row.get('version_binding') == 'recorded'
                and row.get('provenance', {}).get('request_count') == request_count):
            paths.update(value for value in (row.get('locator'), row.get('absolute_path'))
                         if isinstance(value, str) and value)
    return paths


def processed_journal_turns(rows: Sequence[Mapping[str, Any]],
                            compacted_records: Sequence[str] | set[str]) -> list[int]:
    """Closed turns whose every receipt is adopted under its saved policy.

    Owners supply their validated closed journal, including reply-only turns.
    This is presentation coverage, never completion, evidence or note validity.
    No through-turn watermark can stand in for an absent receipt.
    """
    records: dict[int, set[str]] = {}
    for row in rows:
        records.setdefault(row['turn'], set()).add(row['record'])
    covered = set(compacted_records)
    return sorted(turn for turn, originals in records.items() if originals <= covered)


def select_working_context(
    *, user_requests: Sequence[str], system_text: str,
    base_context: Mapping[str, Any],
    turn_blocks: Sequence[Sequence[Mapping[str, Any]]],
    observations: Sequence[Mapping[str, Any]],
    schema_tokens: int, context_token_target: int,
    estimate_tokens: Callable[[str], int],
    work_notes: Sequence[Mapping[str, Any]] = (),
    selected_records: Sequence[str] = (),
    priority_source_paths: Sequence[str] = (),
    current_artifacts: Sequence[str] = (),
    version_resolver: Callable[[str], str | None] | None = None,
    block_request_counts: Sequence[int] | None = None,
    summarized_through_turn: int = 0,
    history_access: str = 'read_history',
    trailing_messages: Sequence[Mapping[str, Any]] = (),
    material_policy: Literal['ranked', 'referenced', 'scoped', 'retained', 'working'] = 'ranked',
    operation_history: Sequence[Mapping[str, Any]] | None = None,
    require_work_notes: bool = False,
    fit_work_notes: bool = False,
    processed_turns: Sequence[int] | None = None,
    archived_turns: Sequence[int] | None = None,
    economical_checkpoint: bool = False,
    economical_after_turn: int = 0,
    sparse_handoffs: bool = False,
    native_observations: Sequence[Mapping[str, Any]] | None = None,
    context_authority: Literal['sources.v1'] | None = None,
    message_projector: Callable | None = None,
    preserve_material_observations: bool = False,
    working_set_policy: Literal['recent.v2', 'recent.v3', 'recent.v4'] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return messages and decision-budget metadata without modifying inputs.

    Complete blocks form a recent suffix using up to half the input target;
    the latest block, all requests and base contracts are mandatory. Counts
    specify how many requests preceded each block; without counts only the
    initial request precedes work, and subsequent feedback follows it.
    ``turn`` indexes the original blocks, including empty boundary slots.
    Rows and notes are supplied claims, not verified facts or authorization.
    The resolver receives absolute_path or path; None means unknown. Stale
    rows are excluded from material selection and marked in retained native
    work. Excerpts address original character ranges, not invented offsets.
    Context (including any checkpoint/through_turn) and inputs stay unchanged.
    Schema cost and mandatory trailing instructions are included in selection;
    final optional admissions converge against the complete serialized input.
    Mandatory overflow remains visible for callers to enforce hard budgets.
    Trailing messages cannot add
    assistant/tool work outside its atomic history block.

    ``referenced`` retains owner notes beside recent native work and admits
    archived observations only by selected_records or note records. Notes do
    not certify evidence or replace historical coverage; summarized_through_turn
    has no effect under this policy. Referenced material stays whole or is
    omitted for capacity, without lexical retrieval or automatic backfill.
    Explicit operation_history enables a compact receipt navigation overview
    alongside notes, native work and referenced original bodies, all admitted
    by this selector's same measured input budget. Whole entries retain their
    original provenance and historical versions; they are never evidence text,
    read coverage, current certificates or permission. No operation is merged
    with another receipt, including repeated reads, versions and failures.
    Every requested record has model-visible supply status, including missing
    records and all windows of a partially supplied record.

    ``retained`` independently considers supplied historical observations with
    matching file versions and historical operation results, without requiring
    a note or explicit reference. It keeps whole recorded bodies and admits
    them before optional navigation under the same input target. The caller's
    historical_observation role is a provenance claim: a matching file digest
    does not itself establish that the recorded text is an actual read window.
    authored_text snapshots with write_context retain version-bound authored
    material separately, including empty files; they never establish read evidence.
    ``require_work_notes`` includes all supplied notes in mandatory context
    before optional material admission, independently of original body supply.
    With usable required notes, retained current/referenced material precedes
    optional older native work. The latest complete block remains mandatory;
    cold starts and historical optional-note policies retain their suffix rule.
    ``fit_work_notes`` is the journal working policy: notes receive priority
    within the same measured input instead of an unbounded mandatory envelope.
    Current materials and exact operation results remain selectable without a
    successful note snapshot. Compaction pressure accounts for omitted whole
    work, including authored arguments and reasoning, not just readable sources.
    ``processed_turns`` is optional journal-owner bookkeeping: these operations
    follow the saved checkpoint contract. Notes marked citations.v1 require
    supporting citations actually supplied in this input to retire history;
    unmarked historical notes retain their original admission-based policy.
    Neither policy independently verifies note semantics. Explicit/current
    originals remain selectable.
    ``economical_checkpoint`` requests optional journal compaction when repeated
    older work outweighs mandatory input, without shrinking the model window.
    The saved failure boundary prevents re-buying a failed summary for little
    new work; originals remain available if no usable checkpoint is adopted.
    ``sparse_handoffs`` retires individually cited closed turns while retaining
    recent work and explicitly selected/current originals; uncited gaps stay.

    ``working`` uses that same journal and version admission, but current
    production names only prioritize material: a usable supplied handoff may
    retire its repeated body. Explicit selections and the latest atomic turn
    remain original input. Inventory membership never pins a body indefinitely.
    ``archived_turns`` separately names complete receipts actually supplied to
    usable checkpoints. With supplied active notes these may leave routine
    presentation without retaining every old citation. This is neither evidence
    coverage nor a claim of understanding; explicit selections and recent atomic
    work stay available and the original journal remains retrievable.

    ``native_observations`` contains transient source locators derived by the
    message owner from actual presentation. Explicit supply matches receipt,
    field, recorded version and range, never text elsewhere in a block. Stale
    native bytes remain historical and marked stale. None retains the saved
    legacy text-inference contract for consumers not migrated to this policy.

    ``context_authority`` leaves the historical layout unchanged when None.
    sources.v1 presents the same derived envelope at index 1 as assistant
    content with source scope metadata and structurally empty reasoning.
    Original requests and atomic native history retain their own roles/order.
    """
    if context_authority is not None and context_authority != 'sources.v1':
        raise ValueError('Unknown context authority policy')
    if working_set_policy not in (None, 'recent.v2', 'recent.v3', 'recent.v4'):
        raise ValueError('Unknown working set policy')
    recent_set = working_set_policy is not None
    active_material_policy = working_set_policy in {'recent.v3', 'recent.v4'}
    # None preserves historical consumers. An explicit (possibly empty) supply
    # is derived by the message owner, never inferred from matching prose.
    def source_key(row: Mapping[str, Any]) -> str:
        return json.dumps([row.get(key) for key in (
            'turn', 'record', 'field', 'call_id', 'kind', 'role', 'path',
            'absolute_path', 'sha256', 'offset_bytes', 'next_offset_bytes', 'chunk_id')],
            ensure_ascii=False)

    native_sources = {source_key(row) for row in native_observations or ()}
    if schema_tokens < 0 or context_token_target < 0:
        raise ValueError('Token costs and target must be nonnegative')
    if type(economical_after_turn) is not int or economical_after_turn < 0:
        raise ValueError('Invalid economical checkpoint history boundary')
    if material_policy not in ('ranked', 'referenced', 'scoped', 'retained', 'working'):
        raise ValueError('Unknown working context selection policy')
    scoped_records = material_policy == 'scoped'
    retained_records = material_policy in {'retained', 'working'}
    working_records = material_policy == 'working'
    if recent_set and not working_records:
        raise ValueError('Recent working set requires the journal working material policy')
    if fit_work_notes and not (retained_records and require_work_notes):
        raise ValueError('Fitted notes require the retained journal working policy')
    owner_records = material_policy in {'referenced', 'scoped'}
    whole_records = owner_records or retained_records
    context = deepcopy(dict(base_context))
    if 'working_view' in context:
        raise ValueError('base_context reserves working_view for this selector')
    blocks = deepcopy(list(turn_blocks))
    rows = deepcopy(list(observations))
    notes = deepcopy(list(work_notes))
    if require_work_notes:
        # Version projection belongs to the caller; hashes are internal bindings,
        # not model-visible scientific verification or supplied original bodies.
        for note in notes:
            note.pop('observed_versions', None)
    tail = deepcopy(list(trailing_messages))
    operations = deepcopy(list(operation_history or ()))
    operation_indices: list[int] = []
    if any(message.get('role') not in {'user', 'system'}
           or not isinstance(message.get('content'), str)
           or set(message) != {'role', 'content'} for message in tail):
        raise ValueError('Trailing instructions must be user/system text messages')
    if type(summarized_through_turn) is not int or not 0 <= summarized_through_turn <= len(blocks):
        raise ValueError('Summary coverage must be within the original journal')
    if whole_records:
        summarized_through_turn = 0
    # Only a usable note snapshot can replace repeated historical presentation.
    # This retires model input, never observations, required findings or sources.
    if not any(note.get('current') is not False and note.get('status') in
               (None, 'current', 'active', 'ok') for note in notes):
        summarized_through_turn = 0
    counts = (list(block_request_counts) if block_request_counts is not None else
              [min(1, len(user_requests))] * len(blocks))
    if (len(counts) != len(blocks) or any(type(n) is not int or not 0 <= n <= len(user_requests)
                                       for n in counts) or counts != sorted(counts)):
        raise ValueError('Request counts must align with blocks and be chronological')
    for block in blocks:
        pending: set[str] = set()
        for message in block:
            if message.get('role') == 'assistant':
                if pending:
                    raise ValueError('Incomplete tool call/result block')
                calls = message.get('tool_calls') or []
                pending = {call['id'] for call in calls}
                if len(pending) != len(calls):
                    raise ValueError('Duplicate tool call id')
            elif message.get('role') == 'tool':
                call_id = message.get('tool_call_id')
                if call_id not in pending:
                    raise ValueError('Tool result without its call')
                pending.remove(call_id)
            elif pending:
                raise ValueError('Interrupted tool call/result block')
        if pending:
            raise ValueError('Incomplete tool call/result block')

    def dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

    def is_request(message: Mapping[str, Any]) -> bool:
        if message.get('role') != 'user':
            return False
        content = message.get('content')
        if content in user_requests:
            return True
        try:
            request = json.loads(content).get('user_request', {})
            index = request.get('index')
            return type(index) is int and 0 <= index < len(user_requests) and request.get('text') == user_requests[index]
        except (ValueError, TypeError, AttributeError):
            return False

    blocks = [[message for message in block if not is_request(message)] for block in blocks]
    capacity_omitted: set[int] = set()
    economical_history: set[int] = set()
    processed = set(processed_turns or ())
    archived = set(archived_turns or ())
    if archived_turns is not None and (not working_records or any(
            type(i) is not int or not 0 <= i < len(blocks) for i in archived)):
        raise ValueError('Working archive requires original working journal turns')
    journal_selection = processed_turns is not None
    if journal_selection and not fit_work_notes:
        raise ValueError('Processed journal turns require fitted work notes')
    if type(sparse_handoffs) is not bool or sparse_handoffs and not journal_selection:
        raise ValueError('Sparse handoffs require a journal selection')
    if working_records and not (journal_selection and sparse_handoffs):
        raise ValueError('Working material requires the sparse journal handoff policy')
    def active(note: Mapping[str, Any]) -> bool:
        return note.get('current') is not False and note.get('status') in (
            None, 'current', 'active', 'ok')

    material_first = retained_records and (fit_work_notes or require_work_notes and any(
        active(note) and isinstance(note.get('text'), str) and note['text'].strip() for note in notes))
    latest = next((i for i in reversed(range(len(blocks))) if blocks[i]), None)
    recent = [latest] if latest is not None else []
    native = [message for i in recent for message in blocks[i]]
    if latest is not None and not material_first:
        for i in reversed(range(min(summarized_through_turn,latest),latest)):
            if not blocks[i]:
                continue
            candidate = blocks[i] + native
            if estimate_tokens(dump(candidate)) > (context_token_target if fit_work_notes else context_token_target // 2):
                break
            recent.insert(0, i)
            native = candidate

    def locator(row: Mapping[str, Any]) -> dict[str, Any]:
        return {key: deepcopy(value) for key, value in row.items()
                if key not in {'text'}}

    explicit_references = set(selected_records)
    references = set(explicit_references)
    for note in notes:
        if (whole_records and not journal_selection) or active(note):
            references.update(note.get('records') or [])
    # Under retained, note records can guide priority without turning every
    # optional note reference into mandatory model-visible bookkeeping.
    reported_references = explicit_references if retained_records else references
    artifacts = set(current_artifacts)
    query = ('' if owner_records else ' '.join(user_requests) + ' ' + ' '.join(
        str(note.get('text', '')) for note in notes if active(note)))
    terms = set(re.findall(r'\w+', query.casefold()))
    versions: dict[str, str | None] = {}
    states: list[dict[str, Any]] = []
    keys: list[str] = []
    native_texts: set[str] = set()

    def collect_text(value: Any, texts: set[str] | None = None) -> None:
        if texts is None:
            texts = native_texts
        if isinstance(value, str):
            texts.add(value)
            if value.lstrip().startswith(('{', '[')):
                try:
                    collect_text(json.loads(value), texts)
                except ValueError:
                    pass
        elif isinstance(value, Mapping):
            texts.add(dump(value))
            for item in value.values():
                collect_text(item, texts)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect_text(item, texts)

    for row in rows:
        text = row.get('text')
        empty_source = (row.get('kind') == 'source_text' and row.get('size_bytes') == 0
                        and row.get('offset_bytes') == 0 and row.get('next_offset_bytes') == 0)
        empty_authored = (row.get('kind') == 'authored_text' and row.get('role') == 'write_context'
                          and row.get('size_bytes') == 0 and row.get('offset_bytes') == 0
                          and row.get('next_offset_bytes') == 0)
        valid = isinstance(text, str) and bool(row.get('record')) and (bool(text) or empty_source or empty_authored)
        path = row.get('absolute_path') or row.get('path')
        digest = row.get('sha256')
        version = ('historical' if row.get('kind') == 'operation_result' else
                   'stale' if row.get('current') is False or row.get('status') == 'stale' else 'unchecked')
        if retained_records and version not in {'historical', 'stale'}:
            if (version_resolver is None or not isinstance(path, str) or not path
                    or not isinstance(digest, str) or not digest):
                version = 'unknown'
            else:
                if path not in versions:
                    versions[path] = version_resolver(path)
                current = versions[path]
                version = ('unknown' if not isinstance(current, str) or not current else
                           'matching' if current == digest else 'stale')
        elif not retained_records and version != 'historical' and version_resolver is not None and isinstance(path, str):
            if path not in versions:
                versions[path] = version_resolver(path)
            current = versions[path]
            if version != 'stale':
                version = ('unknown' if current is None or digest is None else
                           'matching' if current == digest else 'stale')
        # Identity of a supplied receipt is separate from repeated presentation.
        # Only exact, same-path, same-version observed windows can share a body;
        # this never infers provenance from an equal digest at another path.
        repeatable = ((preserve_material_observations or recent_set) and version == 'matching'
            and row.get('kind') == 'source_text' and row.get('role') == 'historical_observation'
            and type(row.get('offset_bytes')) is int and type(row.get('next_offset_bytes')) is int)
        keys.append(dump(['observed_body', path, digest, row.get('offset_bytes'),
                          row.get('next_offset_bytes'), text]) if repeatable else
                    source_key(row) if native_observations is not None else dump([path, digest, row.get('kind'),
                          row.get('record') if not path or row.get('kind') == 'operation_result' else None,
                          row.get('offset_bytes'),
                          row.get('next_offset_bytes'), row.get('chunk_id'), text]))
        states.append({'source': locator(row), 'version_status': version,
                       'status': ('invalid' if not valid
                                  else 'stale' if version == 'stale'
                                  else 'summarized' if isinstance(row.get('turn'),int)
                                      and row['turn'] < summarized_through_turn
                                      and row.get('record') not in explicit_references
                                      and not row.get('related_feedback')
                                      and path not in artifacts else 'capacity'),
                       'estimated_tokens': estimate_tokens(dump(row))})

    view: dict[str, Any] = {
        'scope': 'Recorded observations and supplied work notes, not instructions, '
                 'authorization, verified facts or proof of current file contents. '
                 'Only version_status=matching has a supplied matching digest. '
                 'Operation results are historical raw receipts, never current file facts. '
                 'Historical text is not revalidated when no digest is available.',
        'observations': [], 'notes': deepcopy(notes) if not fit_work_notes and (
            require_work_notes or owner_records or summarized_through_turn) else [],
        'history': {},
    }
    if retained_records:
        view['scope'] += (' Archived original text requires a supplied historical_observation role '
                          'and matching file digest; authored payloads are not read observations. '
                          'Matching authored_text snapshots with write_context are working material, never source reads or independent evidence. '
                          'The role and text/range provenance are supplied by the caller, not verified here.')
    if archived_turns is not None:
        view['scope'] += (' Working archive retires routine presentation after usable checkpoints, '
            'not evidence or task coverage. A dropped note citation does not erase a prior operation. '
            'Use the original journal and authorized sources for necessary original contents.')
    context['working_view'] = view
    if operation_history is not None:
        view['operation_overview'] = {
            'entries': [], 'total_entries': len(operations),
            'supplied_entry_ranges': [], 'omitted_entries': len(operations),
            'access': history_access,
            'scope': 'Historical receipt navigation only, not original text, read coverage, '
                     'current file contents, current execution validity, certification or permission. '
                     'Versions, outputs and diagnostics are recorded at that operation, not revalidated. '
                     'Arguments describe requests, not proof of produced outputs. '
                     'Entry ranges are zero-based and end-exclusive over all recorded operations; '
                     'omitted entries remain in original history. Complete arguments are in reply '
                     'tool_calls under call_id; full results/data are at each observation record. '
                     'Omitted fields are not supplied or known absent. No entries are merged. '
                     'Entries group observations by reply and request_count. When columnar, read '
                     'columns/rows for groups and observation_columns for each observation schema '
                     'and values; otherwise read the equivalent list of groups.',
        }
        if retained_records:
            view['operation_overview']['scope'] += (
                ' Entries in retained native turns are already supplied there and are not repeated here.')

    reference_indices: dict[str, list[int]] = {}
    related_feedback = {i for i, row in enumerate(rows)
        if row.get('kind') == 'operation_result' and row.get('related_feedback') is True}
    if whole_records:
        for i, row in enumerate(rows):
            if row.get('record') in reported_references:
                reference_indices.setdefault(row['record'], []).append(i)

    def observation_text(row: Mapping[str, Any]) -> str:
        text = row['text']
        if row.get('kind') == 'operation_result':
            try:
                return dump(json.loads(text))
            except ValueError:
                pass
        return text

    def in_native(row: Mapping[str, Any], turns: Sequence[int]) -> bool:
        if row.get('turn') not in turns:
            return False
        if native_observations is not None:
            return source_key(row) in native_sources
        text = observation_text(row)
        return bool(text) and text in native_texts

    def mark_native() -> None:
        native_texts.clear()
        if native_observations is None:
            collect_text([message for i in recent for message in blocks[i]])
        for row, state in zip(rows, states):
            if state['status'] in {'invalid','summarized'}:
                continue
            if in_native(row, recent):
                state['status'] = 'native'
                state.pop('supplied_sources', None)
            elif state['version_status'] == 'stale':
                state['status'] = 'stale'
            elif state['status'] == 'working_view':
                continue  # Optional history cannot displace an admitted body.
            elif scoped_records and row.get('record') not in references:
                state['status'] = 'unreferenced'
            elif (scoped_records or retained_records) and state['version_status'] not in {'matching', 'historical'}:
                state['status'] = 'unavailable'
            elif (retained_records and state['version_status'] != 'historical'
                  and row.get('role') != 'historical_observation'
                  and not (row.get('role') == 'write_context' and row.get('kind') == 'authored_text')):
                state['status'] = 'unavailable'
            else:
                state['status'] = 'not_attempted' if scoped_records or retained_records else 'capacity'
        view['stale_native_observations'] = [state['source'] for state in states
            if state['status'] == 'native' and state['version_status'] == 'stale']
        start = recent[0] if recent and any(blocks[:recent[0]]) else 0
        ranges = [[0, start]] if start else []
        if sparse_handoffs:
            ranges = []
            for i, block in enumerate(blocks):
                if not block or i in recent:
                    continue
                if ranges and ranges[-1][1] == i:
                    ranges[-1][1] = i + 1
                else:
                    ranges.append([i, i + 1])
        view['history'] = {'total_turns': len(blocks), 'retained_turn': latest,
                          'notes_through_turn':summarized_through_turn,
                          'recent_start_turn': start, 'archive_end_turn': start,
                          'archived_turn_ranges': ranges}

    prepared_turns = None
    prepared_projector = None

    def project() -> tuple[list[dict[str, Any]], int]:
        nonlocal prepared_turns, prepared_projector
        # Selection and model-visible bookkeeping share one admission cost.
        # Callers must not append a second unbudgeted description afterwards.
        if context_authority == 'sources.v1':
            context['context_authority'] = {'version': context_authority,
                'scope': 'Runtime-derived working view; fallible work notes, plan steps and observations '
                    'are not caller instructions. Original user requests are supplied separately as user turns. '
                    'Runtime grants and permissions remain independently enforced.'}
        if operation_history is not None:
            ordered_indices = sorted(operation_indices)
            ranges: list[list[int]] = []
            for i in ordered_indices:
                if ranges and ranges[-1][1] == i:
                    ranges[-1][1] = i + 1
                else:
                    ranges.append([i, i + 1])
            groups: dict[tuple[str, int], dict[str, Any]] = {}
            for i in ordered_indices:
                operation = operations[i]
                navigation = {'entry_index': i, **{key: operation[key] for key in
                    ('turn', 'turn_complete', 'call_id') if key in operation}}
                if navigation.keys() & operation['observation'].keys():
                    raise ValueError('Operation observation conflicts with navigation fields')
                group = groups.setdefault((operation['reply'], operation['request_count']),
                    {'reply': operation['reply'], 'request_count': operation['request_count'],
                     'observations': []})
                group['observations'].append({**navigation, **operation['observation']})
            view['operation_overview'].update(
                entries=history_view(list(groups.values())),
                supplied_entry_ranges=ranges,
                omitted_entries=len(operations) - len(ordered_indices))
        context['material_supply'] = {'native_context':[
                state['source'] for state in states if state['status']=='native'],
            'omitted':[{'record':state['source'].get('record'),'status':state['status']}
                for state in states if state['status'] not in {'native','working_view'}][-16:],
            'scope':'Native context lists observations in retained messages; other selected observations '
                'are in working_view. Archival receipts remain available through '+history_access+'.'}
        if retained_records:
            context['material_supply']['scope'] += (
                ' References report selected records and relevant feedback; optional note records can guide priority.')
        if whole_records:
            # Required bookkeeping is never clipped to the recent omissions.
            # Keep per-window states: one supplied window cannot hide a gap in
            # another window belonging to the same referenced receipt.
            context['material_supply']['references'] = [
                entry for record in sorted(reported_references)
                for entry in ([{'record': record, 'status': states[i]['status'],
                                'version_status': states[i]['version_status'],
                                'source': source}
                               for i in reference_indices.get(record, [])
                               for source in states[i].get('supplied_sources', [states[i]['source']])]
                              or [{'record': record, 'status': 'missing'}])]
            context['material_supply']['references'].extend(
                {'record': rows[i]['record'], 'status': states[i]['status'],
                 'version_status': states[i]['version_status'], 'source': states[i]['source']}
                for i in sorted(related_feedback) if rows[i]['record'] not in reported_references)
        cost = 0
        while True:
            context['working_context'] = {'latest_atomic_turn':latest,
                'recent_start_turn':view['history']['recent_start_turn'],
                'estimated_input_tokens':cost,'context_exceeds_input_target':cost>context_token_target,
                **({'compaction_needed':bool((capacity_omitted | economical_history) - processed)}
                   if fit_work_notes else {})}
            if 'resources' in context:
                context['resources'].update(approximate_next_input_tokens=cost,
                    context_exceeds_input_target=cost>context_token_target)
            # A projector consumes the in-memory context and serializes its
            # final presentation. Avoid encoding and immediately decoding the
            # entire working view on every admission trial.
            messages = [{'role':'system','content':system_text},
                        {'role':'user','content':dump(context) if message_projector is None else ''}]
            if context_authority == 'sources.v1':
                messages[1].update(role='assistant', reasoning_content='')
            emitted = 0
            for i in recent:
                messages.extend({'role':'user','content':text} for text in user_requests[emitted:counts[i]])
                emitted = counts[i]
                messages.extend(blocks[i])
            messages.extend({'role':'user','content':text} for text in user_requests[emitted:])
            messages.extend(tail)
            if message_projector is not None:
                selection = tuple(recent)
                if selection != prepared_turns:
                    prepared_projector = message_projector(messages)
                    prepared_turns = selection
                messages = prepared_projector(context)
            measured = schema_tokens+estimate_tokens(json.dumps(messages,ensure_ascii=False))
            # A boolean/decimal-width change can reduce the serialized size
            # by one token. Keep the conservative bound rather than oscillate.
            if measured<=cost:
                return messages,cost
            cost = measured

    mark_native()
    while len(recent) > 1 and project()[1] > context_token_target:
        capacity_omitted.add(recent.pop(0))
        mark_native()
    _, required_cost = project()
    # Undo optional admissions in reverse priority/order against the complete
    # final projection, including supply states and compaction bookkeeping.
    # Initial suffix blocks are optional too; the latest block never is.
    admissions: list[tuple[str, Any]] = [('initial_native', i) for i in reversed(recent[:-1])]

    def native_snapshot() -> tuple:
        return (list(recent), deepcopy(states), deepcopy(view['observations']),
                deepcopy(view['history']), deepcopy(view['stale_native_observations']))

    def restore_native(snapshot: tuple) -> None:
        old_recent, old_states, old_observations, old_history, old_stale = snapshot
        recent[:] = old_recent
        states[:] = old_states
        view['observations'] = old_observations
        view['history'] = old_history
        view['stale_native_observations'] = old_stale

    # Keep a bounded evidence share beside fresh work.
    material_ceiling = (context_token_target if whole_records else
                        min(context_token_target, required_cost + context_token_target // 3))
    note_states = [{'index': i, 'records': deepcopy(note.get('records', [])),
                    'status': 'working_view' if not fit_work_notes and (
                        require_work_notes or owner_records or summarized_through_turn) else 'capacity',
                    'estimated_tokens': estimate_tokens(dump(note))}
                   for i, note in enumerate(notes)]
    # Explicit refs/current artifacts, recent operations, then lexical sources.
    def observed_turn(i: int) -> int:
        turn = rows[i].get('turn')
        return turn if type(turn) is int else -1

    def observed_width(i: int) -> int:
        row = rows[i]
        first, last = row.get('offset_bytes'), row.get('next_offset_bytes')
        return max(0, last - first) if row.get('kind') == 'source_text' and type(first) is int and type(last) is int else 0

    active_material: set[int] = set()
    reusable_material: set[int] = set()
    if active_material_policy:
        # The last two read-bearing decisions identify current working files.
        # Keep matching observations eligible across a note checkpoint, within
        # the existing input budget and without treating them as explicit pins.
        current_reads = [i for i, row in enumerate(rows)
            if row.get('kind') == 'source_text' and row.get('tool') == 'read_file'
            and states[i]['version_status'] == 'matching'
            and 0 <= observed_turn(i) < len(counts)
            and counts[observed_turn(i)] == len(user_requests)]
        read_turns = sorted({observed_turn(i) for i in current_reads})[-2:]
        def location(i: int) -> str:
            return rows[i].get('absolute_path') or rows[i]['path']
        paths = {location(i) for i in current_reads if observed_turn(i) in read_turns}
        active_material = {i for i in current_reads if location(i) in paths}
        reusable_material = active_material
        if working_set_policy == 'recent.v4':
            # A checkpoint is a summary, not proof that earlier originals are
            # no longer useful. Let this request's observed, current sources
            # compete for remaining capacity after recent work.
            reusable_material = {i for i, row in enumerate(rows)
                if row.get('kind') == 'source_text' and states[i]['version_status'] == 'matching'
                and 0 <= observed_turn(i) < len(counts)
                and counts[observed_turn(i)] == len(user_requests)}

    def priority(i: int) -> tuple[int, int, int, int, int]:
        row = rows[i]
        words = set(re.findall(r'\w+', str(row.get('text', '')).casefold()))
        return (int(row.get('record') in references),
                int(row.get('path') in artifacts or row.get('absolute_path') in artifacts),
                int(row.get('kind') == 'operation_result'),
                observed_turn(i) if row.get('kind') == 'operation_result' else len(words & terms), -i)

    represented = {keys[i] for i, state in enumerate(states) if state['status'] == 'native'}

    def supplied_material() -> list[Mapping[str, Any]]:
        return [row for row, state in zip(rows, states)
                if state['status'] == 'native' and state['version_status'] == 'matching'] + [
                    entry['observation'] for entry in view['observations']
                    if entry['version_status'] == 'matching']

    def refresh_material_views() -> None:
        """Replace optional display slices when a whole native block is added."""
        candidates = {source_key(row): i for i, (row, state) in enumerate(zip(rows, states))
            if state['status'] == 'working_view' and row.get('kind') == 'source_text'
            and state['version_status'] == 'matching'}
        ordered_indices = list(dict.fromkeys(candidates[source_key(entry['observation'])]
            for entry in view['observations'] if source_key(entry['observation']) in candidates))
        view['observations'] = [entry for entry in view['observations']
            if source_key(entry['observation']) not in candidates]
        for i in ordered_indices:
            windows = material_window_views(rows[i], supplied_material())
            states[i]['status'] = 'working_view' if windows else 'duplicate'
            states[i].pop('supplied_sources', None)
            if windows:
                states[i]['supplied_sources'] = [locator(window) for window in windows]
                view['observations'].extend({'observation': window, 'version_status': 'matching'}
                    for window in windows)

    def admit(i: int) -> None:
        state = states[i]
        if state['status'] != ('not_attempted' if scoped_records or retained_records else 'capacity'):
            return
        if recent_set and rows[i].get('kind') == 'source_text' and state['version_status'] == 'matching':
            windows = material_window_views(rows[i], supplied_material())
            if not windows:
                state['status'] = 'duplicate'
                return
            count = len(view['observations'])
            view['observations'].extend({'observation': window, 'version_status': 'matching'} for window in windows)
            state.update(status='working_view', supplied_sources=[locator(window) for window in windows])
            if project()[1] > material_ceiling:
                del view['observations'][count:]
                state.pop('supplied_sources')
                state['status'] = 'capacity'
                return
            state['estimated_tokens'] = sum(estimate_tokens(dump(window)) for window in windows)
            admissions.append(('material_windows', i))
            represented.add(keys[i])
            return
        if keys[i] in represented:
            state['status'] = 'duplicate'
            return
        supplied = deepcopy(rows[i])
        if whole_records:
            state['status'] = 'working_view'
        # Wrap selector metadata so no supplied field or claim is overwritten.
        view['observations'].append({'observation': supplied,
                                     'version_status': state['version_status']})
        if project()[1] > material_ceiling:
            view['observations'].pop()
            if whole_records:
                state['status'] = 'capacity'
            if whole_records or supplied.get('kind') == 'operation_result':
                return  # Explicit originals and serialized results stay whole.
            from simple_ar.research.contracts import TextChunk
            from simple_ar.research.store.retrieval import source_chunk_views
            chunk = TextChunk(chunk_id=str(i),document_id=str(rows[i]['record']),
                text=rows[i]['text'],source_path=rows[i].get('path'))
            supplied['coverage'] = 'Excerpt of recorded observation; original coverage metadata applies to the record, not this excerpt'
            size = min(6000, len(supplied['text']))
            while size:
                excerpt, = source_chunk_views([chunk], query=query, max_chars=size)
                supplied.update(text=excerpt['text'], record_text_slice={key: excerpt[key] for key in
                    ('character_start', 'character_end', 'total_characters', 'truncated')})
                view['observations'].append({'observation': supplied, 'version_status': state['version_status']})
                if project()[1] <= material_ceiling:
                    break
                view['observations'].pop()
                size //= 2
            if not size:
                return
        represented.add(keys[i])
        state['source'] = locator(supplied)
        state['estimated_tokens'] = estimate_tokens(dump(supplied))
        state['status'] = 'working_view'
        admissions.append(('observation', (i, supplied)))

    # Spend navigation capacity on the archived work absent from native input
    # first, newest first. Do not permanently privilege old errors or penalize
    # successful source reads for having original text. All receipts remain
    # separate, historical and discoverable; native pairs and notes stay intact.
    def operation_priority(i: int) -> tuple[bool, int]:
        return (operations[i].get('turn') not in recent, i)

    def admit_navigation() -> None:
        for i in sorted(range(len(operations)), key=operation_priority, reverse=True):
            if retained_records and operations[i].get('turn') in recent:
                continue  # Its native block is already present in the input.
            operation_indices.append(i)
            if project()[1] > context_token_target:
                operation_indices.pop()  # Never excerpt a serialized operation.
            else:
                admissions.append(('navigation', i))

    if not scoped_records and not retained_records:
        admit_navigation()

    priority_paths = {path: rank for rank, path in enumerate(dict.fromkeys(priority_source_paths))}
    priority_sources: dict[str, list[int]] = {path: [] for path in priority_paths}
    for i, row in enumerate(rows):
        path = row.get('absolute_path') or row.get('path')
        if row.get('kind') == 'source_text' and path in priority_sources:
            priority_sources[path].append(i)
    for indices in priority_sources.values():
        indices.sort(key=lambda i: (-observed_width(i), i))
    for group in zip_longest(*priority_sources.values()):
        for i in group:
            if i is not None:
                admit(i)

    if working_records:
        # A working set follows actual work, not lexical similarity to the
        # original request or membership in an old delivery inventory. Only
        # explicit selections pin originals ahead of more recent observations.
        # Receipt/field/version identity remains separate from body priority.
        ordered = sorted(range(len(rows)), key=lambda i: (
            int(rows[i].get('record') in explicit_references),
            int(i in active_material),
            int(i in related_feedback),
            observed_turn(i), *([observed_width(i)] if recent_set else []), *priority(i)), reverse=True)
    elif retained_records:
        # Explicit references and current objects precede the rest; original
        # text precedes historical operations within each priority group.
        ordered = sorted(range(len(rows)), key=lambda i: (
            int(rows[i].get('record') in explicit_references or rows[i].get('path') in artifacts
                or rows[i].get('absolute_path') in artifacts),
            int(rows[i].get('record') in references or rows[i].get('path') in artifacts
                or rows[i].get('absolute_path') in artifacts),
            int(rows[i].get('kind') != 'operation_result'), *priority(i)), reverse=True)
    else:
        ordered = ([i for i, row in enumerate(rows) if row.get('record') in references]
                   if owner_records else sorted(range(len(rows)), key=priority, reverse=True))
    current_material = {i for i in ordered if rows[i].get('record') in references
                        or rows[i].get('path') in artifacts or rows[i].get('absolute_path') in artifacts}
    preferred = current_material | {i for i in ordered if rows[i].get('kind') == 'operation_result'}
    # A summary that replaces historical presentation is mandatory context.
    # Optional note admission cannot hide both its history and the notes.
    def admit_notes() -> None:
        for i in sorted(range(len(notes)), key=lambda i: (not active(notes[i]), i)):
            view['notes'].append(deepcopy(notes[i]))
            if project()[1] > material_ceiling:
                view['notes'].pop()
            else:
                note_states[i]['status'] = 'working_view'
                admissions.append(('note', i))

    # Current material and explicit selections take precedence over optional
    # model notes and navigation. They still pass the same version checks.
    if retained_records and fit_work_notes:
        for i in ordered:
            row = rows[i]
            if (row.get('record') in explicit_references or not working_records and (
                    row.get('path') in artifacts or row.get('absolute_path') in artifacts)):
                admit(i)

    if fit_work_notes:
        # Notes are useful judgments, not an unbounded mandatory payload. Actual
        # receipts stay independently selectable when an optional summary fails.
        admit_notes()
    if not require_work_notes and not retained_records and not owner_records and not summarized_through_turn:
        admit_notes()
    if material_first:
        if journal_selection:
            # Only adopted receipt coverage with usable supplied notes can
            # retire a historical body. Empty coverage, new reads and sources
            # outside this journal retain ordinary whole-window admission.
            # Explicit/current originals override retirement, not version checks.
            supplied_handoffs = (archived if archived_turns is not None else processed
                ) if any(active(note) for note in view['notes']) else set()
            if archived_turns is None and any(note.get('handoff_policy') == CITATION_HANDOFF_POLICY for note in notes):
                supplied_citations = {record for note in view['notes'] if active(note)
                    and note.get('handoff_policy') == CITATION_HANDOFF_POLICY
                    and isinstance(note.get('text'), str) and note['text'].strip()
                    for record in note.get('records', [])}
                turn_records: dict[int, set[str]] = {}
                for row in [*rows, *[{'turn':operation.get('turn'),
                        'record':operation['observation'].get('record')} for operation in operations]]:
                    if isinstance(row.get('turn'), int) and row.get('record'):
                        turn_records.setdefault(row['turn'], set()).add(row['record'])
                supplied_handoffs = {turn for turn in processed
                    if turn_records.get(turn) and turn_records[turn] <= supplied_citations}
            elif archived_turns is None and sparse_handoffs:
                supplied_handoffs = set()  # Unmarked notes cannot retire individual turns.
            if supplied_handoffs:
                ordered = [i for i in ordered if rows[i].get('turn') not in supplied_handoffs
                    or rows[i].get('record') in explicit_references
                    or i in related_feedback
                    or i in reusable_material
                    or preserve_material_observations and not recent_set and rows[i].get('kind') in {'source_text', 'authored_text'}
                    or recent_set and (rows[i].get('record') in {
                        record for note in view['notes'] if active(note) for record in note.get('records', ())}
                        or rows[i].get('path') in artifacts or rows[i].get('absolute_path') in artifacts)
                    or not working_records and (rows[i].get('path') in artifacts
                        or rows[i].get('absolute_path') in artifacts)]
            # Retained originals precede optional native history; otherwise an
            # evicted read can lose its only body to unrelated native blocks.
            preferred = set(ordered)
            if recent_set:
                needed = explicit_references | {record for note in view['notes'] if active(note)
                    for record in note.get('records', ())}
                ordered = [i for i in ordered if isinstance(rows[i].get('turn'), int) or latest is None
                    or rows[i].get('record') in needed
                    or i in related_feedback
                    or rows[i].get('path') in artifacts or rows[i].get('absolute_path') in artifacts]
                # Explicit originals were admitted above. Recent complete work
                # now precedes optional supporting original bodies.
                preferred = {i for i in ordered if rows[i].get('record') in explicit_references
                             or i in active_material}
        for i in ordered:
            if i in preferred:
                admit(i)
        # Keep a contiguous, complete suffix only when it fits beside current
        # work. Repeating a body already supplied from its receipt wastes that
        # room; its original operation remains in the navigation/archive.
        protected_turns = set()
        if sparse_handoffs:
            protected_turns.update(row['turn'] for row in rows if isinstance(row.get('turn'), int)
                and (row.get('record') in explicit_references or not working_records and (
                    row.get('path') in artifacts or row.get('absolute_path') in artifacts)))
            protected_turns.update(operation['turn'] for operation in operations
                if isinstance(operation.get('turn'), int) and (
                    operation.get('reply') in explicit_references
                    or operation['observation'].get('record') in explicit_references))
            previous = next((i for i in reversed(range(latest or 0)) if blocks[i]), None)
            if previous is not None:
                protected_turns.add(previous)
        for i in reversed(range(latest or 0)):
            if not blocks[i]:
                continue
            if sparse_handoffs and i in supplied_handoffs and i not in protected_turns:
                continue
            if (not sparse_handoffs and journal_selection
                    and all(n in supplied_handoffs for n in range(i+1) if blocks[n])):
                break  # Already bought continuity; originals remain retrievable.
            texts: set[str] = set()
            if native_observations is None:
                collect_text(blocks[i], texts)
            if (not fit_work_notes and any(keys[j] in represented and (
                    in_native(row, [i]) if native_observations is not None else observation_text(row) in texts)
                    for j, row in enumerate(rows) if isinstance(row.get('text'), str))
                    or not fit_work_notes and estimate_tokens(dump([m for n in [i, *recent] for m in blocks[n]]))
                        > context_token_target // 2):
                break
            snapshot = native_snapshot()
            recent.insert(0, i)
            mark_native()
            if fit_work_notes:
                # Replace an already selected body with its actual native
                # block before measuring. Paying for both creates false pressure.
                view['observations'] = [entry for entry in view['observations']
                    if not in_native(entry['observation'], recent)]
                if recent_set:
                    refresh_material_views()
            if project()[1] > context_token_target:
                # Restore presentation and admission states together. Calling
                # mark_native here would turn settled capacity omissions back
                # into not_attempted and enlarge the model-visible bookkeeping.
                restore_native(snapshot)
                capacity_omitted.update(n for n in range(i+1) if blocks[n])
                break
            admissions.append(('native', snapshot))
            represented.update(keys[j] for j, state in enumerate(states) if state['status'] == 'native')
            if recent_set and i == previous:
                # Keep the latest complete work together, then protect the
                # originals and related feedback before optional older native
                # history consumes the remaining room.
                for j in ordered:
                    if (j in related_feedback or rows[j].get('record') in needed or rows[j].get('path') in artifacts
                            or rows[j].get('absolute_path') in artifacts):
                        admit(j)
        for i in ordered:
            if i not in preferred:
                admit(i)
    else:
        for i in ordered:
            if retained_records or i in preferred:
                admit(i)
        for i in ordered:
            if not retained_records and i not in preferred:
                admit(i)
    if retained_records:
        # As pending statuses become final, their bookkeeping can shrink; a
        # later duplicate may also represent a previously oversized window.
        # Retry against those settled states before spending on optional data.
        for i in ordered:
            if states[i]['status'] == 'capacity':
                states[i]['status'] = 'not_attempted'
                admit(i)
        if not require_work_notes and not fit_work_notes:
            admit_notes()
    if scoped_records or retained_records:
        # Original bodies share this budget and precede optional navigation.
        admit_navigation()

    if required_cost > context_token_target:
        capacity_omitted.clear()  # Old-history compaction cannot fix required-input overflow.
    if economical_checkpoint and journal_selection and latest is not None and required_cost <= context_token_target:
        # Do not cap material to a fraction of the model window. A checkpoint
        # becomes worthwhile when replayed, unprocessed history outweighs the
        # actual mandatory input and has already survived more than one turn.
        # Until a checkpoint succeeds, the original native blocks remain intact.
        # Keep the immediately preceding action together with the current
        # action: it may have just supplied a large source for the next step.
        previous = [i for i in recent if i != latest]
        repeated = [i for i in previous[:-1] if i not in processed]
        # After a settled unusable checkpoint, count only newly repeated work:
        # a tiny new action must not buy the same failed summary each turn.
        new_work = [i for i in repeated if i >= economical_after_turn]
        replay_cost = estimate_tokens(dump([message for i in new_work for message in blocks[i]]))
        made_progress = (not active_material_policy or not economical_after_turn or any(
            operation.get('turn', -1) >= economical_after_turn
            and operation['observation'].get('status') == 'ok'
            and (operation['observation'].get('tool') in {
                'write_file', 'edit_file', 'copy_file', 'copy_project',
                'work_document', 'export_report', 'review_result', 'deliver_result', 'inspect_artifacts'}
                or operation['observation'].get('tool') == 'run_process'
                and operation['observation'].get('artifacts'))
            for operation in operations))
        if repeated and replay_cost > required_cost and made_progress:
            economical_history.update(repeated)

    def settle_projection() -> tuple[list[dict[str, Any]], int]:
        supplied_keys = {keys[i] for i, state in enumerate(states)
                         if state['status'] in {'native', 'working_view'}}
        for i, state in enumerate(states):
            if state['status'] == 'duplicate' and keys[i] not in supplied_keys:
                if recent_set and rows[i].get('kind') == 'source_text' and not material_window_views(
                        rows[i], supplied_material()):
                    continue
                state['status'] = 'capacity'
        if journal_selection:
            for state in states:
                if state['status'] == 'not_attempted':
                    state['status'] = 'not_requested'
        return project()

    messages, cost = settle_projection()
    while cost > context_token_target and admissions:
        # One convergence path for every optional presentation. Each step
        # removes a whole admitted unit, never a whole file's other windows,
        # and remeasures actual serialized input rather than reserving slack.
        kind, value = admissions.pop()
        if kind == 'navigation':
            operation_indices.remove(value)
        elif kind == 'note':
            view['notes'].remove(notes[value])
            note_states[value]['status'] = 'capacity'
        elif kind == 'observation':
            index, supplied = value
            view['observations'].remove({'observation': supplied,
                                        'version_status': states[index]['version_status']})
            states[index].update(source=locator(rows[index]), status='capacity',
                                 estimated_tokens=estimate_tokens(dump(rows[index])))
        elif kind == 'material_windows':
            view['observations'] = [entry for entry in view['observations']
                if source_key(entry['observation']) != source_key(rows[value])]
            states[value].pop('supplied_sources', None)
            states[value].update(source=locator(rows[value]), status='capacity')
        elif kind == 'native':
            removed = set(recent) - set(value[0])
            restore_native(value)
            capacity_omitted.update(removed)
        else:
            recent.remove(value)
            capacity_omitted.add(value)
            mark_native()
        messages, cost = settle_projection()
    if cost > context_token_target:
        # No optional admission remains: report the actual mandatory envelope,
        # including final bookkeeping, without clipping requests or native work.
        capacity_omitted.clear()
        economical_history.clear()
        messages, cost = settle_projection()
        required_cost = cost
    else:
        required_cost = min(required_cost, cost)
    # Once an unprocessed closed turn actually falls out of the window,
    # checkpoint the other closed unsummarized turns as one bounded batch too.
    # Otherwise each subsequent turn can evict one more block and force a
    # complete rewrite of the same notes. Latest work stays verbatim; the
    # checkpoint owner still limits each batch to its real input budget.
    compaction_turns = ([i for i, block in enumerate(blocks)
        if block and i != latest and i not in processed]
        if capacity_omitted - processed else sorted(economical_history - processed))
    selected = [{**{key: value for key, value in state.items() if key != 'supplied_sources'}, 'source': source}
        for state in states if state['status'] in {'native', 'working_view'}
        for source in state.get('supplied_sources', [state['source']])]
    omitted = [state for state in states if state['status'] not in {'native', 'working_view'}]
    metadata = {
        'input_token_target': context_token_target, 'schema_tokens': schema_tokens,
        'estimated_input_tokens': cost, 'required_input_tokens': required_cost,
        'target_overflow_tokens': max(0, cost - context_token_target),
        'context_exceeds_input_target': cost > context_token_target,
        'overflow_reason': (('required_requests_contracts_owner_notes_latest_atomic_turn' if require_work_notes or owner_records else
                             'required_requests_contracts_latest_atomic_turn')
                            if cost > context_token_target else None),
        'latest_atomic_turn': latest, 'selected_records': selected,
        'omitted_records': omitted, 'notes': note_states,
        'selected_turns': list(recent),
        'recent_start_turn': view['history']['recent_start_turn'],
        **({'compaction_needed':context['working_context']['compaction_needed']} if fit_work_notes else {}),
        **({'compaction_turns':compaction_turns,
            'economical_compaction':bool(economical_history - processed)} if journal_selection else {}),
        'archive_end_turn': view['history']['archive_end_turn'],
        'recent_native_tokens': estimate_tokens(dump([message for i in recent for message in blocks[i]])),
        'material_input_ceiling': material_ceiling,
        'omitted_turns': [i for i, block in enumerate(blocks) if block and i not in recent],
        'history_refs': [locator(row) for row in rows],
        'scope': 'Estimates include schema and message framing; no hard budget admission. '
                 'Archived blocks and original records remain caller-owned and unmodified.',
    }
    if operation_history is not None:
        metadata['operation_overview'] = {key: deepcopy(value) for key, value in
            view['operation_overview'].items() if key != 'entries'}
    return messages, metadata


def fit_working_context(*, measure_input: Callable[[list[dict[str, Any]]], int],
                        **arguments: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fit the same selector to a complete request without tokenizing every trial.

    The selector uses its cheap fragment estimate during admission. If the
    completed request exceeds its target, calibrate that estimate against the
    actual request counter and repeat selection. Required input still remains
    visible when it cannot fit; no request or journal is modified here.
    """
    target = arguments['context_token_target']
    estimate = arguments['estimate_tokens']
    schema_cost = arguments['schema_tokens']
    scale = 1.0
    for attempt in range(3):
        options = dict(arguments)
        if scale != 1.0:
            options['estimate_tokens'] = lambda text: math.ceil(estimate(text) * scale)
            options['schema_tokens'] = math.ceil(schema_cost * scale)
        messages, metadata = select_working_context(**options)
        actual = measure_input(messages)
        estimated = metadata['estimated_input_tokens']
        if (actual <= target or metadata['context_exceeds_input_target']
                or attempt == 2 or estimated <= 0):
            break
        scale *= actual / estimated * 1.01
    metadata['estimated_input_tokens'] = actual
    metadata['required_input_tokens'] = min(metadata['required_input_tokens'], actual)
    metadata['target_overflow_tokens'] = max(0, actual - target)
    metadata['context_exceeds_input_target'] = actual > target
    if actual <= target:
        metadata['overflow_reason'] = None
    elif not metadata['overflow_reason']:
        metadata['overflow_reason'] = 'required_or_estimation_overflow'
    return messages, metadata
