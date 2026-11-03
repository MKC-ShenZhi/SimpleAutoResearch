"""Durable caller input in the existing session submission journal.

Reception never writes actions, the manifest or the ledger. The execution owner
alone adopts inputs under its session mutation lock; adoption and request index
are committed together in actions.json. A short independent reception lock uses
the same OS lock and atomic file writer, so accepting text cannot race a tool
result or wait for a provider call to finish.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from simple_ar.core.capabilities import ArtifactStore
from simple_ar.core.locking import SessionFileLock


class FeedbackSubmissions:
    path = 'tools/submissions/feedback.json'

    def __init__(self, store: ArtifactStore) -> None:
        self.store = store

    @staticmethod
    def validate(submission_id: str, feedback: str) -> None:
        if not isinstance(submission_id, str) or not submission_id.strip():
            raise ValueError('Feedback requires a stable nonempty submission_id')
        if not isinstance(feedback, str) or not feedback.strip():
            raise ValueError('Feedback requires nonempty text')

    def records(self) -> list[dict]:
        if not self.store.exists(self.path):
            return []
        journal = self.store.read_json(self.path)
        if journal.get('schema_version') != 'feedback_submissions.v1' or not isinstance(journal.get('submissions'), list):
            raise ValueError('Unsupported feedback submission journal')
        rows = journal['submissions']
        seen = set()
        for row in rows:
            self.validate(row['submission_id'], row['feedback'])
            if row['submission_id'] in seen:
                raise ValueError('Duplicate feedback submission in retained journal')
            seen.add(row['submission_id'])
        return rows

    def accept(self, submission_id: str, feedback: str) -> dict:
        self.validate(submission_id, feedback)
        with SessionFileLock(self.store.root / 'tools/submissions/.feedback.lock'):
            rows = self.records()
            existing = next((row for row in rows if row['submission_id'] == submission_id), None)
            if existing is not None:
                if existing['feedback'] != feedback:
                    raise ValueError('Feedback submission_id already exists with different text')
                return existing
            row = {'submission_id': submission_id, 'feedback': feedback,
                'received_at': datetime.now(timezone.utc).isoformat(timespec='microseconds')}
            self.store.write_json(self.path, {'schema_version': 'feedback_submissions.v1',
                'submissions': [*rows, row]}, kind='caller_submission')
            return row

    @staticmethod
    def receipt(row: dict, state: dict[str, Any]) -> dict:
        adoption = state.get('feedback_submissions', {}).get(row['submission_id'], {})
        status = adoption.get('status', 'pending')
        if status not in {'pending', 'applied'}:
            raise ValueError('Unsupported feedback adoption status')
        receipt = {**row, 'received': True, 'status': status}
        if status == 'applied':
            index = adoption.get('request_index')
            if (type(index) is not int or not 0 < index < len(state['requests'])
                    or state['requests'][index] != row['feedback']):
                raise ValueError('Adopted feedback differs from its original request journal')
            receipt['request_index'] = index
        else:
            receipt['reason'] = adoption.get('reason', 'Waiting for a safe execution boundary')
        return receipt

    def receipts(self, state: dict[str, Any]) -> list[dict]:
        return [self.receipt(row, state) for row in self.records()]
