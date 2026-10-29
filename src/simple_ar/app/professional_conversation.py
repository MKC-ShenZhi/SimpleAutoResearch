"""Shared professional input, receipt continuity and model-call admission.

Owners supply their original journals and terminal contracts. This module owns
no task, permissions, verdict, mutable memory or independent budget ledger.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable

from simple_ar.integrations.llm import estimate_tokens


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
              operations_for_turn: Callable, failure: dict | None = None) -> dict:
        """Use only owner-validated, settled checkpoint artifacts.

        Processed receipts are not facts or complete-turn coverage. Projection
        of note versions is an owner responsibility, separate from this replay.
        """
        from simple_ar.app.work_memory import summary_coverage
        if not snapshot or not snapshot.get('notes'):
            return {**previous, **({'attempted_through_turn':failure['attempted_through_turn']}
                                  if failure else {})}
        compacted = set(previous.get('compacted_records', ()))
        for row in view['observations']:
            operations = operations_for_turn(row['turn'])
            if (row.get('truncated') is False and row['record'] in operations
                    and row.get('arguments', {}) == operations[row['record']]):
                compacted.add(row['record'])
        return {'notes':snapshot['notes'], 'compacted_records':compacted,
            'through_turn':summary_coverage(previous=previous, snapshot=snapshot,
                view=view, operations_for_turn=operations_for_turn)}

    def tokens(self, value: ConversationInput) -> int:
        return (estimate_tokens(json.dumps(value.messages, ensure_ascii=False))
            + estimate_tokens(json.dumps(value.tools, ensure_ascii=False))
            + (self.client.effective_output_limit() or 0))

    def prepare(self, project: Callable[[bool], ConversationInput], *,
                followup: Callable[[ConversationInput], dict],
                checkpoint: Callable[[ConversationInput, int], None] | None = None,
                closing_enabled: bool = True, force_close: bool = False,
                continue_allowed: bool = True, additional: bool = False) -> ConversationInput:
        """Project, optionally compact closed work, then protect a real exit.

        The owner chooses the terminal contract and any non-budget boundary.
        Rebuild after a checkpoint so neither new notes nor final instructions
        are appended outside the measured input. No provider call is retried.
        """
        if {'llm_requests', 'total_tokens'}.intersection(self.ledger.unknown_dimensions(
                unbounded_only=True, current_allowance_only=True)):
            raise ValueError('Professional conversation capacity is unknown; no request sent')

        def admission(value):
            floor = followup(value)
            slots, capacity = self.ledger.remaining('llm_requests'), self.ledger.remaining('total_tokens')
            protect = bool(floor and continue_allowed and not force_close
                and (slots is None or slots >= 1 + floor.get('llm_requests', 0))
                and (capacity is None or capacity >= self.tokens(value) + floor.get('total_tokens', 0)))
            return floor, protect

        value = project(False)
        floor, protect = admission(value)
        closing = force_close or closing_enabled and not protect
        working = value.projection.get('working_context', value.projection)
        if checkpoint is not None and not closing and working.get('compaction_needed', True):
            # Nested inspection must also leave the caller's existing floor.
            # The compactor adds its decision/note protection inside this floor.
            with self.ledger.protect_remaining(floor if protect else {}, additional=additional):
                checkpoint(value, self.tokens(value))
            value = project(False)
            floor, protect = admission(value)
            closing = force_close or closing_enabled and not protect
        if closing:
            value = project(True)
        value.closing = closing
        value.reserve = floor if protect and not closing else {}
        return value

    def call(self, value: ConversationInput, *, label: str, call_id: str,
             native: bool = False, additional: bool = False) -> dict:
        with self.ledger.protect_remaining(value.reserve or {}, additional=additional):
            turn = self.client.chat_turn if native else (
                getattr(self.client, 'tool_turn', None) or self.client.chat_turn)
            return turn(value.messages, tools=value.tools, label=label, budget_call_id=call_id)
