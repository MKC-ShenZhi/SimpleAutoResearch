"""Atomic request-freeze and response-retention helper shared across document work and writing."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from simple_ar.core.capabilities import ArtifactStore


def retained_response(
    store: ArtifactStore,
    path: str,
    request: dict[str, Any],
    call: Callable[[], Any] | None = None,
    *,
    retry_unstarted: bool = False,
    unstarted: Callable[[Exception], bool] | None = None,
) -> Any:
    """Freeze the request and retain its response before domain validation."""
    def completion(response: Any) -> dict[str, str]:
        return {
            key: hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            for key, value in (('request_sha256', request), ('response_sha256', response))
        }

    record_contract = request.get('response_record_contract')
    atomic = record_contract in {'atomic_complete.v1', 'atomic_complete.v2'}

    def check_normalization(record: dict, parsed: Any) -> None:
        from simple_ar.integrations.llm import normalize_terminal_json_object
        if (
            record_contract != 'atomic_complete.v2'
            or not isinstance(record, dict)
            or set(record) != {'contract', 'raw_response', 'raw_sha256', 'appended_suffix'}
            or record['contract'] != 'terminal_json_closure.v1'
            or not isinstance(record['raw_response'], str)
            or hashlib.sha256(record['raw_response'].encode()).hexdigest() != record['raw_sha256']
        ):
            raise ValueError('Retained response syntax normalization changed; no replay')
        normalized = normalize_terminal_json_object(record['raw_response'])
        if (
            normalized is None
            or not normalized[1]
            or normalized != (parsed, record['appended_suffix'])
        ):
            raise ValueError('Retained response differs from its exact terminal normalization')

    if store.exists(path):
        if store.read_json(path) != request:
            raise ValueError('Article/review input changed; preserve it and use a new identity')
        if not store.exists('response.json'):
            if not (retry_unstarted and call is not None and store.exists('not_dispatched.json')):
                raise ValueError('Article/review request failed or is unknown; inspect its ledger, no blind replay')
            # A new authorized attempt may dispatch a locally rejected request.
            # Remove the marker first so an interrupted dispatch cannot replay it.
            store.resolve('not_dispatched.json').unlink()
        else:
            if request.get('format_correction_contract') and store.exists('response_error.json'):
                raise ValueError('Article/review request failed or is unknown; inspect its ledger, no blind replay')
            response = store.read_json('response.json')
            if atomic:
                keys = {'response', 'request_sha256', 'response_sha256'}
                if record_contract == 'atomic_complete.v2' and isinstance(response, dict) and 'normalization' in response:
                    keys.add('normalization')
                if (
                    not isinstance(response, dict)
                    or set(response) != keys
                    or {key: response[key] for key in ('request_sha256', 'response_sha256')} != completion(response['response'])
                ):
                    raise ValueError('Retained atomic response changed or has no complete bound record; no blind replay')
                if 'normalization' in response:
                    check_normalization(response['normalization'], response['response'])
                return response['response']
            if request.get('format_correction_contract') and (
                not store.exists('response_complete.json')
                or store.read_json('response_complete.json') != completion(response)
            ):
                raise ValueError('Retained review response is changed or not known complete; no blind replay')
            return response
    if call is None:
        raise ValueError('Article/review needs a model client or a saved complete response')
    if not store.exists(path):
        store.write_json(path, request)
    normalization = None
    try:
        response = call()
    except Exception as exc:
        from simple_ar.integrations.llm import LLMResponseError, normalize_terminal_json_object
        raw = getattr(exc, 'raw_response', None)
        normalized = (
            normalize_terminal_json_object(raw)
            if record_contract == 'atomic_complete.v2'
            and isinstance(exc, LLMResponseError)
            and exc.correction_allowed
            and exc.finish_reason in {None, 'stop'}
            and isinstance(raw, str)
            else None
        )
        if normalized is None or not normalized[1]:
            if raw is not None:
                store.write_json('response.json', {'raw_response': raw})
            if unstarted is not None and unstarted(exc):
                store.write_json('not_dispatched.json', {'reason': 'budget'})
            elif request.get('format_correction_contract'):
                store.write_json('response_error.json', {'type': type(exc).__name__, 'error': str(exc)})
            raise
        response, suffix = normalized
        normalization = {
            'contract': 'terminal_json_closure.v1',
            'raw_response': raw,
            'raw_sha256': hashlib.sha256(raw.encode()).hexdigest(),
            'appended_suffix': suffix,
        }
    store.write_json(
        'response.json',
        {'response': response, **completion(response), **({'normalization': normalization} if normalization else {})}
        if atomic
        else response,
    )
    if request.get('format_correction_contract') and not atomic:
        store.write_json('response_complete.json', completion(response))
    return response
