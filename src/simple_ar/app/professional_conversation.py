"""Shared professional input, receipt continuity and model-call admission.

Owners supply their original journals and terminal contracts. This module owns
no task, permissions, verdict, mutable memory or independent budget ledger.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable

from simple_ar.integrations.llm import check_llm_call_stop, estimate_tokens
from simple_ar.core.working_context import RECEIPT_SUPPLY_POLICIES


# Production contracts consume the same retained observations and working memory.
RETAINED_PROFESSIONAL_CONTRACTS = frozenset({'work.v5', 'work.v6', 'work.v7', 'work.v8'})


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
        return (estimate_tokens(json.dumps(value.messages, ensure_ascii=False))
            + estimate_tokens(json.dumps(value.tools, ensure_ascii=False))
            + (self.client.effective_output_limit() or 0))

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
            slots, capacity = self.ledger.remaining('llm_requests'), self.ledger.remaining('total_tokens')
            protect = bool(floor and continue_allowed and not force_close
                and (slots is None or slots >= 1 + floor.get('llm_requests', 0))
                and (capacity is None or capacity >= self.tokens(value) + floor.get('total_tokens', 0)))
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
