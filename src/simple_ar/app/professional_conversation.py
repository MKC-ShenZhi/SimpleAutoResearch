"""Shared professional input, receipt continuity and model-call admission.

Owners supply their original journals and terminal contracts. This module owns
no task, permissions, verdict, mutable memory or independent budget ledger.
"""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import json
from typing import Any, Callable

from simple_ar.integrations.llm import check_llm_call_stop, estimate_native_input_tokens
from simple_ar.core.working_context import RECEIPT_SUPPLY_POLICIES


# Production contracts consume the same retained observations and working memory.
RETAINED_PROFESSIONAL_CONTRACTS = frozenset({'work.v5', 'work.v6', 'work.v7', 'work.v8', 'work.v9', 'work.v10', 'work.v11', 'work.v12'})
NATIVE_HISTORY_POLICY = 'closed_observations.v1'
INTEGRATED_WORK_LIFECYCLES = frozenset({'integrated.v1', 'integrated.v2', 'integrated.v3'})


def continuation_blocks(blocks: list[list[dict]], *, policy: str | None, thinking_mode: str) -> list[list[dict]]:
    """Keep unusable provider history as closed observations, without replay.

    Only new owners opt in. Original replies and tool outcomes are untouched;
    no provider reasoning is supplied or invented. Atomic tool-call/result
    closure is required before changing their presentation to historical data.
    """
    if policy not in (None, NATIVE_HISTORY_POLICY):
        raise ValueError('Unknown native history policy')
    if policy is None or thinking_mode != 'enabled':
        return blocks
    projected = []
    for block in blocks:
        missing = any(row.get('role') == 'assistant' and not any(
            isinstance(row.get(key), str) for key in ('reasoning_content', 'reasoning')) for row in block)
        if not missing:
            projected.append(block)
            continue
        calls = [call['id'] for row in block if row.get('role') == 'assistant'
                 for call in row.get('tool_calls') or []]
        results = [row['tool_call_id'] for row in block if row.get('role') == 'tool']
        if len(set(calls)) != len(calls) or len(set(results)) != len(results) or set(calls) != set(results):
            raise ValueError('Unclosed native work cannot become historical observations')
        replacement, observations = [], []

        def flush():
            if observations:
                replacement.append({'role': 'user', 'content': json.dumps({
                    'closed_journal_observations': deepcopy(observations),
                    'scope': 'Recorded assistant work and tool outcomes, not a new user request, '
                        'instructions, verified evidence or operations to repeat. Original replies '
                        'remain in the journal. Provider reasoning was absent; continue from actual '
                        'recorded outcomes without replaying their effects.'}, ensure_ascii=False)})
                observations.clear()

        for row in block:
            if row.get('role') in {'assistant', 'tool'}:
                observations.append(row)
            else:
                flush()
                replacement.append(row)
        flush()
        projected.append(replacement)
    return projected


def recorded_work_messages(messages: list[dict]) -> list[dict]:
    """Closed operations are observations, not an inherited action agenda."""
    if len(messages) < 2:
        return messages
    context = json.loads(messages[1]['content'])
    calls, records, requests = {}, [], []
    for message in messages[2:]:
        if message['role'] == 'assistant':
            for call in message.get('tool_calls', []):
                calls[call['id']] = call
        elif message['role'] == 'tool':
            records.append({'action':calls[message['tool_call_id']], 'observation':message['content']})
        elif message['role'] == 'user':
            requests.append(message)
    context.pop('work_plan', None)
    context['recorded_work'] = {'observations':records,
        'scope':'Verbatim closed operation arguments and observations. These are historical '
            'work records, not next actions or new requirements. Full replies remain in read_history.'}
    return [messages[0], {**messages[1], 'content':json.dumps(context, ensure_ascii=False)}, *requests]


def _work_unit_context(message: dict, context: dict, count: int) -> dict:
    context.pop('work_plan', None)
    context['recorded_work'] = {'unit_count':count,
        'scope':'Actual assistant statements, operations and observed results in closed work units. '
            'Statements are fallible historical judgments, not user requests, new obligations or '
            'verified facts. Do not replay their effects. Original replies remain in read_history.'}
    return {**message, 'content':json.dumps(context, ensure_ascii=False)}


def work_unit_messages(messages: list[dict]) -> list[dict]:
    """Present complete native work once, without serializing JSON inside JSON.

    The shared selector chooses whole closed units before calling this pure
    projector. Provider reasoning is neither a request nor required replay;
    original messages and invalid argument strings stay in the journal.
    """
    if len(messages) < 2:
        return messages
    context = json.loads(messages[1]['content'])
    projected, current, count = [], None, 0

    def decoded(text):
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return text  # Invalid provider arguments remain actual observations.

    def flush():
        nonlocal current, count
        if current is None:
            return
        if {row['id'] for row in current['operations']} != {
                row['call_id'] for row in current['observations']}:
            raise ValueError('Unclosed native work cannot become recorded work')
        projected.append({'role':'user', 'content':json.dumps(
            {'recorded_work_unit':current}, ensure_ascii=False)})
        current = None
        count += 1

    def retain(rows):
        nonlocal current
        for row in rows:
            if row['role'] == 'assistant':
                flush()
                current = {'content':row.get('content'), 'operations':[
                    {'id':call['id'], 'tool':call['function']['name'],
                     'arguments':decoded(call['function']['arguments'])}
                    for call in row.get('tool_calls') or []], 'observations':[]}
            elif row['role'] == 'tool':
                if current is None or row['tool_call_id'] not in {
                        call['id'] for call in current['operations']}:
                    raise ValueError('Work observation has no matching native work unit')
                current['observations'].append({'call_id':row['tool_call_id'],
                    'result':decoded(row['content'])})
            else:
                flush()
                projected.append(row)

    # Only roles and call identities define work. User text may itself contain
    # any JSON keys; it must never be mistaken for an owner-created wrapper.
    retain(messages[2:])
    flush()
    return [messages[0], _work_unit_context(messages[1], context, count), *projected]


def work_unit_projector() -> Callable[[list[dict]], list[dict]]:
    """Reuse unchanged work units while one working-set selection tries its options."""
    previous, projected, count = None, None, 0

    def project(messages: list[dict]) -> list[dict]:
        nonlocal previous, projected, count
        if len(messages) < 2:
            return work_unit_messages(messages)
        history = messages[2:]
        if previous != history:
            result = work_unit_messages(messages)
            previous = deepcopy(history)
            projected = deepcopy(result[2:])
            count = json.loads(result[1]['content'])['recorded_work']['unit_count']
            return result
        context = json.loads(messages[1]['content'])
        return [messages[0], _work_unit_context(messages[1], context, count), *deepcopy(projected)]

    return project


@dataclass
class ConversationInput:
    messages: list
    tools: list
    projection: dict
    closing: bool = False
    reserve: dict | None = None


class ProfessionalConversation:
    def __init__(self, client: Any):
        self.client = client
        self.ledger = client._budget_ledger

    @staticmethod
    def adopt(*, previous: dict, snapshot: dict | None, view: dict,
              operations_for_turn: Callable, failure: dict | None = None,
              archive: bool = False) -> dict:
        """Use only owner-validated, settled checkpoint artifacts.

        New marked checkpoints require surviving note citations. Unmarked
        checkpoints replay their historical admission-based contract.
        Projection of note versions is an owner responsibility, separate from
        this replay; citations do not verify note semantics.
        Current checkpoints replace knowledge and bounded supporting references;
        their attempted scope prevents repurchasing history, never claims coverage.
        """
        from simple_ar.app.work_memory import summary_coverage, summary_records
        receipt_supply = view.get('native_supply_policy') in RECEIPT_SUPPLY_POLICIES
        # Purchase scope is not citation coverage. Reconstruct it from the same
        # immutable inputs, never from the candidate end or surviving notes.
        attempted = ({'attempted_through_turn':max(previous.get('attempted_through_turn', 0),
                         max(row['turn'] for row in view['observations'])+1),
            'attempted_records':set(previous.get('attempted_records', ())) |
                {row['record'] for row in view['observations']}} if receipt_supply else {})
        if not snapshot or not snapshot.get('notes'):
            return {**previous, **({'attempted_through_turn':failure['attempted_through_turn']}
                                  if failure and not receipt_supply else {}), **attempted}
        compacted = summary_records(previous=previous, snapshot=snapshot,
            view=view, operations_for_turn=operations_for_turn)
        # Presentation retirement is not citation coverage. A settled usable
        # checkpoint may archive complete actually supplied receipts even when
        # its fallible working understanding drops their old citations. All
        # originals remain in the owner's journal and can be selected again.
        archived = ({'archived_records': summary_records(
            previous={'compacted_records': previous.get('archived_records', ())},
            snapshot=snapshot, view={**view, 'handoff_policy': None},
            operations_for_turn=operations_for_turn)} if archive else {})
        return {'notes':snapshot['notes'], 'compacted_records':compacted,
            'through_turn':summary_coverage(previous=previous, snapshot=snapshot,
                view=view, operations_for_turn=operations_for_turn), **attempted, **archived}

    def tokens(self, value: ConversationInput) -> int:
        return (estimate_native_input_tokens(self.client, value.messages, value.tools)
            + (self.client.effective_output_limit() or 0))

    def request_resources(self, value: ConversationInput) -> dict[str, int]:
        """Measure one offered request with the client's unchanged output cap."""
        return {'llm_requests': 1, 'total_tokens': self.tokens(value)}

    @staticmethod
    def resource_floor(stages: dict[str, dict[str, int | float]]) -> dict[str, int | float]:
        """Compose owner-declared sequential work on the original ledger.

        Stages describe actual downstream obligations, such as revision,
        resubmission, applicable review and an honest exit after rejection.
        They are forecasts, never a new allowance, a call-count quality limit
        or a promise that the work will pass. Unknown costs must not be passed
        as zero; owners retain the limitations of their measured lower bounds.
        """
        from collections.abc import Mapping
        from math import isfinite

        if not isinstance(stages, Mapping):
            raise ValueError('Resource stages must be an owner-declared mapping')
        result = {}
        for name, resources in stages.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(resources, Mapping):
                raise ValueError('Resource stages require names and resource mappings')
            for dimension, amount in resources.items():
                if (not isinstance(dimension, str) or not dimension.strip()
                        or type(amount) not in (int, float) or not isfinite(amount) or amount < 0):
                    raise ValueError('Resource forecasts must be known nonnegative finite amounts')
                result[dimension] = result.get(dimension, 0) + amount
        return result

    def can_protect(self, value: ConversationInput, floor: dict) -> bool:
        """Measure a next-step floor on the one real ledger, across dimensions."""
        current = self.request_resources(value)
        return all((remaining := self.ledger.remaining(dimension)) is None
            or remaining >= amount + current.get(dimension, 0)
            for dimension, amount in floor.items())

    def prepare(self, project: Callable[[bool], ConversationInput], *,
                followup: Callable[[ConversationInput], dict],
                checkpoint: Callable[[ConversationInput, int], None] | None = None,
                closing_enabled: bool = True, force_close: bool = False,
                continue_allowed: bool = True, additional: bool = False,
                exit_first: bool = False) -> ConversationInput:
        """Project, optionally compact closed work, then protect a real exit.

        The owner chooses the terminal contract and any non-budget boundary.
        Rebuild after a checkpoint so neither new notes nor final instructions
        are appended outside the measured input. No provider call is retried.
        ``exit_first`` measures the owner's executable exit before ordinary
        projection and uses that exact input/output estimate as the follow-up
        floor. Historical owners keep their original projection order.
        """
        if {'llm_requests', 'total_tokens'}.intersection(self.ledger.unknown_dimensions(
                unbounded_only=True, current_allowance_only=True)):
            raise ValueError('Professional conversation capacity is unknown; no request sent')

        exit_value = None

        def admission(value):
            floor = followup(exit_value if exit_value is not None else value)
            protect = bool(floor and continue_allowed and not force_close
                and self.can_protect(value, floor))
            return floor, protect

        def ordinary_input():
            nonlocal exit_value
            if exit_first:
                # Measure a real exit before ordinary projection/admission can
                # consume the last opportunity. This does not retry a failed
                # projection, shrink output limits or reserve provider usage.
                exit_value = project(True)
                if force_close or not admission(exit_value)[1]:
                    return exit_value
            return project(False)

        value = ordinary_input()
        floor, protect = admission(value)
        closing = force_close or closing_enabled and not protect
        working = value.projection.get('working_context', value.projection)
        if checkpoint is not None and not closing and working.get('compaction_needed', True):
            # Nested inspection must also leave the caller's existing floor.
            # The compactor adds its decision/note protection inside this floor.
            with self.ledger.protect_remaining(floor if protect else {}, additional=additional):
                checkpoint(value, self.tokens(value))
            value = ordinary_input()
            floor, protect = admission(value)
            closing = force_close or closing_enabled and not protect
        if closing:
            value = exit_value if exit_first else project(True)
        value.closing = closing
        value.reserve = floor if protect and not closing else {}
        return value

    def call(self, value: ConversationInput, *, label: str, call_id: str,
             native: bool = False, additional: bool = False) -> dict:
        check_llm_call_stop()
        with self.ledger.protect_remaining(value.reserve or {}, additional=additional):
            turn = self.client.chat_turn if native else (
                getattr(self.client, 'tool_turn', None) or self.client.chat_turn)
            return turn(value.messages, tools=value.tools, label=label, budget_call_id=call_id)
