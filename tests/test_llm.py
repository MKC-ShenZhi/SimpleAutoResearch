from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import asyncio
import base64
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from PIL import Image

from simple_ar.integrations.usage import record_usage, summarize_usage
from simple_ar.core.artifacts import read_json, read_jsonl
from simple_ar.core.budget import BudgetConflictError, BudgetExceededError, BudgetLedger, BudgetUnknownError
from simple_ar.integrations.llm import (
    LLMClient,
    LLMDeadlineError,
    LLMError,
    LLMImageInputError,
    LLMResponseError,
    LLMRequest,
    LLMSettings,
    LLMUsage,
    _call_openai_sdk,
    _request_for_api_mode,
    estimate_tokens,
    parse_json_object,
)


class LLMParsingTests(unittest.TestCase):
    def setUp(self):
        catalog = patch('simple_ar.integrations.llm.load_model_catalog', return_value=None)
        catalog.start()
        self.addCleanup(catalog.stop)

    def test_local_vision_payloads_and_provider_or_unknown_usage(self):
        from simple_ar.integrations.model_profiles import ModelCatalog, ModelProfile
        from simple_ar.integrations.llm import ESTIMATED_IMAGE_INPUT_TOKENS
        with tempfile.TemporaryDirectory() as folder:
            paths = tuple(Path(folder) / f'image-{fmt}.not-an-image-extension' for fmt in ('PNG', 'JPEG', 'WEBP'))
            for path, fmt in zip(paths, ('PNG', 'JPEG', 'WEBP')):
                Image.new('RGB', (16, 12), 'white').save(path, format=fmt)
            for mode, streamed in (('chat', False), ('responses', False), ('chat', True)):
                for supplied_usage in (True, False):
                    with self.subTest(mode=mode, stream=streamed, usage=supplied_usage):
                        profile = ModelProfile(api='openai_chat' if mode == 'chat' else 'openai_responses',
                            model='fixture-vision', base_url='https://fixture.invalid/v1', api_key_env='FIXTURE_KEY',
                            capabilities=['vision'], stream=streamed)
                        catalog = ModelCatalog(profiles={'view': profile}, routes={'vision': 'view'})
                        ledger = BudgetLedger({'llm_requests': 2, 'total_tokens': 40000},
                            storage_path=Path(folder) / f'{mode}-{streamed}-{supplied_usage}.json')
                        observed = []
                        with patch('simple_ar.integrations.llm.load_dotenv'), \
                             patch('simple_ar.integrations.llm.load_model_catalog', return_value=catalog), \
                             patch.dict(os.environ, {'FIXTURE_KEY': 'fixture'}):
                            client = LLMClient.from_env(purpose='vision', max_output_tokens=3000,
                                budget_ledger=ledger, usage_callback=observed.append)
                        self.assertEqual(client._profile_name, 'view')
                        usage = {'prompt_tokens': 100, 'completion_tokens': 5, 'total_tokens': 105}
                        response = {'choices': [{'message': {'content': '{"findings":[]}'}}]}
                        if mode == 'responses':
                            response = {'output_text': '{"findings":[]}'}
                        if supplied_usage:
                            response['usage'] = usage
                        if streamed:
                            chunks = [{'choices': [{'delta': {'content': '{"findings":[]}'}, 'finish_reason': 'stop'}]}]
                            if supplied_usage:
                                chunks.append({'choices': [], 'usage': usage})
                            response = iter(chunks)
                        with patch('simple_ar.integrations.llm._call_openai_sdk', return_value=response) as sdk:
                            self.assertEqual(client.ask_json('review', 'Goal: no text', image_paths=paths), {'findings': []})
                        request = sdk.call_args.args[1]
                        rows = request['messages'] if mode == 'chat' else request['input']
                        blocks = rows[-1]['content']
                        self.assertEqual(blocks[0]['type'], 'text' if mode == 'chat' else 'input_text')
                        self.assertIn('Goal: no text', blocks[0]['text'])
                        for block, path, mime in zip(blocks[1:], paths, ('image/png', 'image/jpeg', 'image/webp')):
                            url = block['image_url']['url'] if mode == 'chat' else block['image_url']
                            self.assertTrue(url.startswith(f'data:{mime};base64,'))
                            self.assertEqual(base64.b64decode(url.split(',', 1)[1]), path.read_bytes())
                        self.assertEqual(_request_for_api_mode(request, mode).get('messages' if mode == 'chat' else 'input'), rows)
                        if not streamed and supplied_usage:
                            with patch('openai.OpenAI') as factory:
                                create = factory.return_value.chat.completions.create if mode == 'chat' else factory.return_value.responses.create
                                create.return_value = response
                                self.assertEqual(_call_openai_sdk(mode, request), response)
                                self.assertEqual(create.call_args.kwargs['messages' if mode == 'chat' else 'input'], rows)
                                self.assertEqual(factory.call_args.kwargs['max_retries'], 0)
                                factory.return_value.close.assert_called_once()
                        with self.assertRaisesRegex(LLMError, 'cannot switch API'):
                            _request_for_api_mode(request, 'responses' if mode == 'chat' else 'chat')
                        entry = ledger.entries[0]
                        self.assertEqual(sdk.call_count, 1)
                        self.assertEqual(entry.reserved['total_tokens'], 3 * ESTIMATED_IMAGE_INPUT_TOKENS
                            + estimate_tokens('review') + estimate_tokens(blocks[0]['text']) + 3000)
                        self.assertEqual(observed[0].estimated_image_input_tokens, 3 * ESTIMATED_IMAGE_INPUT_TOKENS)
                        restored = BudgetLedger.load(ledger.storage_path)
                        if supplied_usage:
                            self.assertEqual(entry.status, 'settled')
                            self.assertEqual(entry.actual_source, 'provider')
                            self.assertEqual(observed[0].total_tokens, 105)
                        else:
                            self.assertEqual(entry.status, 'unknown')
                            self.assertEqual(entry.actual, {'llm_requests': 1})
                            self.assertIsNone(restored.remaining('total_tokens'))
                            self.assertEqual(restored.remaining('llm_requests'), 1)
                            self.assertEqual(observed[0].source, 'unknown')
                            self.assertIsNone(observed[0].total_tokens)
                            self.assertIsNone(observed[0].estimated_cost_usd)
                            summary = summarize_usage([observed[0].to_row()])
                            self.assertIsNone(summary['total_tokens'])
                            self.assertEqual(summary['unknown_usage_requests'], 1)
                            with patch('simple_ar.integrations.llm._call_openai_sdk') as again, self.assertRaises(LLMError):
                                client.ask('review', 'again', image_paths=paths)
                            again.assert_not_called()

    def test_vision_preflight_rejects_invalid_inputs_modes_and_implicit_text_profile(self):
        from simple_ar.integrations.model_profiles import ModelCatalog, ModelProfile
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            valid, corrupt, unsupported, large = (root / name for name in ('valid.png', 'fake.png', 'image.gif', 'large.png'))
            Image.new('RGB', (10, 10)).save(valid)
            corrupt.write_bytes(b'not image pixels')
            Image.new('RGB', (10, 10)).save(unsupported)
            Image.new('RGB', (4097, 1024)).save(large)
            profile = ModelProfile(api='openai_chat', model='fixture-vision', base_url='https://fixture.invalid/v1',
                api_key_env='FIXTURE_KEY', capabilities=['vision'])
            text = profile.model_copy(update={'capabilities': ['text']})
            for i, (paths, mode, purpose, selected, cap) in enumerate((
                    ((corrupt,), 'chat', 'vision', profile, 3000),
                    ((unsupported,), 'chat', 'vision', profile, 3000),
                    ((large,), 'chat', 'vision', profile, 3000),
                    ((root / 'missing.png',), 'chat', 'vision', profile, 3000),
                    ((valid,) * 5, 'chat', 'vision', profile, 3000),
                    ((valid,), 'auto', 'vision', profile, 3000),
                    ((valid,), 'chat', 'text', profile, 3000),
                    ((valid,), 'chat', 'vision', text, 3000),
                    ((valid,), 'chat', 'vision', profile, None),
                    ([], 'chat', 'vision', profile, 3000))):
                with self.subTest(case=i):
                    ledger = BudgetLedger({'llm_requests': 1, 'total_tokens': 40000})
                    client = LLMClient(LLMSettings(api_key='fixture', api_mode=mode, max_output_tokens=cap),
                        model_catalog=ModelCatalog(profiles={'view': selected}), profile_name='view', purpose=purpose,
                        budget_ledger=ledger)
                    with patch('simple_ar.integrations.llm._call_openai_sdk') as sdk, self.assertRaises(LLMError) as failure:
                        client.ask_json('review', 'goal', image_paths=paths)
                    if paths == (large,):
                        self.assertIsInstance(failure.exception, LLMImageInputError)
                        self.assertIn('4194304 pixels', str(failure.exception))
                        self.assertIn('preserve the original', str(failure.exception))
                    self.assertEqual(ledger.entries, ())
                    sdk.assert_not_called()
            with patch('simple_ar.integrations.llm.MAX_VISION_BYTES', 1), self.assertRaises(LLMError):
                client.ask('review', 'goal', image_paths=(valid,))
            with patch('simple_ar.integrations.llm.load_dotenv'), \
                 patch('simple_ar.integrations.llm.load_model_catalog', return_value=None), self.assertRaisesRegex(LLMError, 'named profile'):
                LLMClient.from_env(purpose='vision')
            implicit = LLMClient(LLMSettings(api_key='fixture', api_mode='chat'), purpose='vision')
            with patch('simple_ar.integrations.llm._call_openai_sdk') as sdk, self.assertRaises(LLMError):
                implicit.ask('review', 'goal', image_paths=(valid,))
            sdk.assert_not_called()

    def test_proxy_is_api_transport_only_for_sync_and_async_sdk(self):
        proxy = "http://127.0.0.1:7890"
        async def chunks():
            yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}
        class Stream:
            close = AsyncMock()
            def __aiter__(self):
                return chunks()
        for mode, streamed in (("chat", False), ("responses", False), ("chat", True)):
            with self.subTest(mode=mode, streamed=streamed), patch.dict(os.environ, {"API_PROXY": proxy}), \
                    patch("openai.OpenAI") as sync_factory, patch("openai.AsyncOpenAI") as async_factory, \
                    patch("openai.DefaultHttpxClient") as sync_http, \
                    patch("openai.DefaultAsyncHttpxClient") as async_http:
                environment = dict(os.environ)
                settings = LLMSettings(api_key="fixture", api_mode=mode, stream=streamed,
                                       request_timeout_sec=1, proxy_env="API_PROXY")
                client = LLMClient(settings)
                if streamed:
                    sdk, transport = async_factory.return_value, async_http
                    sdk.chat.completions.create = AsyncMock(return_value=Stream())
                    sdk.close = AsyncMock()
                else:
                    sdk, transport = sync_factory.return_value, sync_http
                    sdk.chat.completions.create.return_value = {"choices": [{"message": {"content": "ok"}}]}
                    sdk.responses.create.return_value = {"output_text": "ok"}
                self.assertEqual(client.ask("system", "user"), "ok")
                transport.assert_called_once()
                self.assertEqual(transport.call_args.kwargs['http2'], False)
                self.assertEqual(transport.call_args.kwargs['proxy'], proxy)
                self.assertEqual(set(transport.call_args.kwargs),
                    {'http2', 'proxy', 'event_hooks'})
                create = sdk.responses.create if mode == "responses" else sdk.chat.completions.create
                self.assertNotIn("proxy", create.call_args.kwargs)
                self.assertNotIn("proxy_env", create.call_args.kwargs)
                self.assertNotIn(proxy, json.dumps(create.call_args.kwargs))
                self.assertEqual(settings.proxy_env, "API_PROXY")
                self.assertNotIn(proxy, repr(settings))
                self.assertNotIn(proxy, repr(vars(settings)))
                self.assertEqual(dict(os.environ), environment)
                (sync_factory if streamed else async_factory).assert_not_called()

    def test_proxy_preflight_precedes_budget_and_legacy_fake_signature_is_unchanged(self):
        subprocess.run([sys.executable, '-c',
            'import sys; from simple_ar.cli.main import main; '
            'from simple_ar.integrations.llm import LLMClient, LLMSettings; '
            'LLMClient(LLMSettings(api_key="fixture")); assert "litellm" not in sys.modules'],
            check=True, timeout=20, capture_output=True)
        ledger = BudgetLedger({"llm_requests": 1})
        client = LLMClient(LLMSettings(api_key="fixture", transport_backend="litellm"),
                           budget_ledger=ledger)
        with patch.dict(sys.modules, {"litellm": None}), \
                patch("simple_ar.integrations.llm._call_provider") as send:
            with self.assertRaisesRegex(LLMError, r"simple-autoresearch\[litellm\]"):
                client.ask("system", "user")
            send.assert_not_called()
            self.assertEqual(ledger.entries, ())
        for value in (None, "http://secret@proxy", "https://proxy/?secret"):
            with self.subTest(value=value), patch.dict(os.environ, {}, clear=True):
                if value is not None:
                    os.environ["API_PROXY"] = value
                ledger = BudgetLedger({"llm_requests": 1})
                client = LLMClient(LLMSettings(api_key="fixture", proxy_env="API_PROXY"), budget_ledger=ledger)
                with patch("simple_ar.integrations.llm._call_provider") as send:
                    with self.assertRaises(LLMError) as caught:
                        client.ask("system", "user")
                self.assertNotIn("secret", str(caught.exception))
                send.assert_not_called()
                self.assertEqual(ledger.entries, ())
        with self.assertRaisesRegex(LLMError, "SDK transport only"):
            LLMClient(LLMSettings(api_key="fixture", transport_backend="litellm", proxy_env="API_PROXY"))
        def legacy_fake(mode, request):
            return {"choices": [{"message": {"content": "ok"}}]}
        with patch("simple_ar.integrations.llm._call_openai_sdk", side_effect=legacy_fake) as send:
            self.assertEqual(LLMClient(LLMSettings(api_key="fixture", api_mode="chat")).ask("s", "u"), "ok")
        self.assertEqual(send.call_args.kwargs, {})

    def test_proxy_failure_diagnostics_and_transport_logs_omit_resolved_url(self):
        proxy = "http://127.0.0.1:7890"
        ledger = BudgetLedger({"llm_requests": 2, "total_tokens": 100})
        client = LLMClient(LLMSettings(api_key="fixture", api_mode="chat", proxy_env="API_PROXY",
            max_output_tokens=10, retry_attempts=2), budget_ledger=ledger)
        logger = logging.getLogger("httpcore.proxy_fixture")
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.setLevel, old_level)
        def fail(**kwargs):
            logger.debug("connection to %s", proxy)
            raise TimeoutError("connection to " + proxy)
        with patch.dict(os.environ, {"API_PROXY": proxy}), patch("openai.OpenAI") as factory, \
                patch("openai.DefaultHttpxClient"), patch("simple_ar.integrations.llm.time.sleep"), \
                self.assertLogs(level=logging.DEBUG) as logs:
            factory.return_value.chat.completions.create.side_effect = fail
            with self.assertRaises(LLMError) as caught:
                client.ask("system", "user")
        self.assertEqual(logger.level, logging.DEBUG)
        self.assertNotIn(proxy, "\n".join(logs.output))
        self.assertNotIn(proxy, str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual([entry.status for entry in ledger.entries], ["unknown", "unknown"])
        self.assertTrue(all(proxy not in entry.reason for entry in ledger.entries))
        # Safe errors must still preserve the existing JSON-format fallback.
        with patch.dict(os.environ, {"API_PROXY": proxy}), \
                patch("simple_ar.integrations.llm._call_openai_sdk", side_effect=[
                    ValueError("response_format not supported at " + proxy),
                    {"choices": [{"message": {"content": '{"ok":true}'}}]},
                ]) as send:
            uncapped = LLMClient(LLMSettings(api_key="fixture", api_mode="chat", proxy_env="API_PROXY"))
            self.assertEqual(uncapped.ask_json("system", "user"), {"ok": True})
            self.assertEqual(send.call_count, 2)

    def test_sdk_stream_deadline_retains_charges_and_bounds_retries_without_api_switch(self):
        for blocked_at in ("connect", "headers", "partial", "heartbeats"):
            with self.subTest(blocked_at=blocked_at), patch("openai.AsyncOpenAI") as factory, \
                    patch("openai.DefaultAsyncHttpxClient") as transport, \
                    patch("openai.OpenAI") as sync_factory, \
                    patch("simple_ar.integrations.llm._api_attempt_order", return_value=["chat", "responses"]), \
                    patch("simple_ar.integrations.llm.time.sleep") as retry_sleep:
                async def chunks():
                    yield {"choices": [{"delta": {"content": '{"looks_complete":true}'}}]}
                    if blocked_at == "heartbeats":
                        while True:
                            await asyncio.sleep(0.001)
                            yield {"choices": []}
                    await asyncio.Event().wait()

                class Stream:
                    close = AsyncMock()
                    def __aiter__(self):
                        return chunks()

                async def create(**kwargs):
                    request = SimpleNamespace(extensions={})
                    await transport.call_args.kwargs['event_hooks']['request'][0](request)
                    trace = request.extensions['trace']
                    await trace('connection.connect_tcp.started', {'secret': 'secret-trace-fixture'})
                    if blocked_at != 'connect':
                        await trace('http11.send_request_body.complete', {'secret': 'secret-trace-fixture'})
                        await trace('http11.receive_response_headers.started', {})
                    # Unrecognized event names and their contents must not leak.
                    await trace('secret-trace-fixture', {'secret': 'secret-trace-fixture'})
                    if blocked_at in {'connect', 'headers'}:
                        await asyncio.Event().wait()
                    return Stream()

                sdk = factory.return_value
                sdk.chat.completions.create = AsyncMock(side_effect=create)
                sdk.close = AsyncMock()
                ledger = BudgetLedger({"llm_requests": 3, "total_tokens": 100})
                client = LLMClient(LLMSettings(api_key="fixture", api_mode="auto", stream=True,
                    request_timeout_sec=0.03, max_output_tokens=10, retry_attempts=1,
                    http2=True), budget_ledger=ledger)
                with self.assertRaises(LLMDeadlineError) as caught:
                    client.ask_json("system", "user")
                self.assertNotIn("looks_complete", str(caught.exception))
                self.assertNotIn('secret-trace-fixture', str(caught.exception))
                self.assertEqual(caught.exception.public_diagnostics(), {
                    'transport_phase': 'connection.connect_tcp.started' if blocked_at == 'connect'
                        else 'http11.receive_response_headers.started',
                    'stream_opened': blocked_at not in {'connect', 'headers'}})
                self.assertIn('transport_phase=' + ('connection.connect_tcp.started'
                    if blocked_at == 'connect' else 'http11.receive_response_headers.started'), str(caught.exception))
                self.assertIn(f"stream_opened={blocked_at not in {'connect', 'headers'}}", str(caught.exception))
                self.assertIn('finish_received=False', str(caught.exception))
                self.assertIn('tool_argument_chars=0', str(caught.exception))
                self.assertIn(f"content_chars={0 if blocked_at in {'connect', 'headers'} else 23}", str(caught.exception))
                self.assertEqual([row.status for row in ledger.entries], ["unknown"])
                self.assertEqual(ledger.entries[0].actual, {"llm_requests": 1})
                self.assertEqual(ledger.unknown_dimensions(), ("total_tokens",))
                self.assertEqual(ledger.remaining("total_tokens"),
                                 100 - ledger.entries[0].reserved["total_tokens"])
                sdk.chat.completions.create.assert_awaited_once()
                sdk.close.assert_awaited_once()
                self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
                transport.assert_called_once()
                self.assertEqual(transport.call_args.kwargs['http2'], True)
                self.assertEqual(set(transport.call_args.kwargs), {'http2', 'event_hooks'})
                sync_factory.assert_not_called()
                retry_sleep.assert_not_called()
                if blocked_at not in {'connect', 'headers'}:
                    Stream.close.assert_awaited_once()

        for cap in (None, 10):
            with self.subTest(output_cap=cap), patch("simple_ar.integrations.llm._call_provider") as send, \
                    patch("simple_ar.integrations.llm.time.sleep") as retry_sleep:
                send.side_effect = [LLMDeadlineError("deadline"),
                    {"choices": [{"message": {"content": '{"ok":true}'}}],
                     "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}]
                ledger = BudgetLedger({"llm_requests": 3, "total_tokens": 100})
                client = LLMClient(LLMSettings(api_key="fixture", api_mode="chat",
                    max_output_tokens=cap, retry_attempts=3), budget_ledger=ledger)
                if cap is None:
                    with self.assertRaises(LLMDeadlineError):
                        client.ask_json("system", "user")
                    self.assertEqual(send.call_count, 1)
                    self.assertIsNone(ledger.remaining("total_tokens"))
                    retry_sleep.assert_not_called()
                else:
                    self.assertEqual(client.ask_json("system", "user"), {"ok": True})
                    self.assertEqual(send.call_count, 2)
                    self.assertEqual([row.status for row in ledger.entries], ["unknown", "settled"])
                    self.assertEqual(ledger.remaining("total_tokens"), 100 - ledger.entries[0].reserved["total_tokens"] - 8)
                    retry_sleep.assert_called_once()

        with patch("simple_ar.integrations.llm._call_provider", side_effect=LLMDeadlineError("deadline")) as send, \
                patch("simple_ar.integrations.llm._api_attempt_order", return_value=["chat", "responses"]), \
                patch("simple_ar.integrations.llm.time.sleep"):
            ledger = BudgetLedger({"llm_requests": 3, "total_tokens": 300})
            client = LLMClient(LLMSettings(api_key="fixture", api_mode="auto",
                max_output_tokens=10, retry_attempts=3), budget_ledger=ledger)
            with self.assertRaises(LLMDeadlineError):
                client.ask("system", "user")
            self.assertEqual(send.call_count, 3)
            self.assertTrue(all(row.status == "unknown" for row in ledger.entries))
            self.assertTrue(all(call.args[1] == "chat" for call in send.call_args_list))

    def test_async_finished_content_keeps_usage_tail_or_completes_without_it(self):
        real_timeout = asyncio.timeout
        for tail in ("usage", "wall_deadline", "tail_deadline", "network_error"):
            with self.subTest(tail=tail), patch("openai.AsyncOpenAI") as factory, \
                    patch("openai.DefaultAsyncHttpxClient"), \
                    patch("simple_ar.integrations.llm.threading.Thread") as thread:
                unexpected_eof = []
                async def chunks():
                    yield {"choices": [{"delta": {"content": "complete"}, "finish_reason": "stop"}]}
                    if tail == "usage":
                        yield {"choices": [], "usage": {
                            "prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
                        unexpected_eof.append(True)
                    elif tail == "network_error":
                        raise TimeoutError("fixture tail error")
                    else:
                        await asyncio.Event().wait()

                class Stream:
                    close = AsyncMock()
                    def __aiter__(self):
                        return chunks()

                sdk = factory.return_value
                sdk.chat.completions.create = AsyncMock(return_value=Stream())
                sdk.close = AsyncMock()
                observed = []
                ledger = BudgetLedger({"llm_requests": 3})
                client = LLMClient(LLMSettings(api_key="fixture", api_mode="chat", stream=True,
                    request_timeout_sec=0.03 if tail == "wall_deadline" else 1,
                    retry_attempts=3), usage_callback=observed.append, budget_ledger=ledger)
                # Shorten only the two-second grace for a cheap deterministic test.
                with patch("simple_ar.integrations.llm.asyncio.timeout", side_effect=lambda seconds:
                           real_timeout(0.01 if seconds == 2.0 and tail == "tail_deadline" else seconds)) as timeout:
                    self.assertEqual(client.ask("system", "user"), "complete")
                self.assertIn(call(2.0), timeout.call_args_list)
                self.assertFalse(unexpected_eof)
                self.assertEqual(observed[0].source, "provider" if tail == "usage" else "estimated")
                if tail == "usage":
                    self.assertEqual(observed[0].total_tokens, 5)
                self.assertEqual([row.status for row in ledger.entries], ["settled"])
                sdk.chat.completions.create.assert_awaited_once()
                Stream.close.assert_awaited_once()
                sdk.close.assert_awaited_once()
                thread.assert_not_called()

    def test_async_network_timeout_keeps_normal_retry_policy(self):
        from simple_ar.integrations.llm import _ChatStreamState, _collect_chat_stream_async, LLMStreamError
        from openai import APIError
        import httpx

        # The same SDK error remains retryable/unknown; diagnostics may name
        # overload but must not disclose arbitrary provider text or secrets.
        for body in ({"message": "upstream overloaded secret-fixture"},
                     {"error": {"message": "upstream overloaded secret-fixture"}},
                     {"message": "other failure secret-fixture"},
                     {"error": {"code": "server_error", "type": "internal_error",
                                "message": "other failure secret-fixture"}},
                     {"code": "secret-fixture", "type": "secret-fixture",
                      "message": "other failure secret-fixture"}):
            with self.subTest(body=body):
                error = APIError("generic stream failure", request=httpx.Request("POST", "https://invalid.test"), body=body)
                error.status_code = 503
                async def failed_chunks():
                    yield {"choices": [{"delta": {"role": "assistant"}}]}
                    raise error
                with self.assertRaises(LLMStreamError) as caught:
                    asyncio.run(_collect_chat_stream_async(failed_chunks(), _ChatStreamState()))
                self.assertNotIn("secret-fixture", str(caught.exception))
                self.assertIn("HTTP status=503", str(caught.exception))
                self.assertEqual("provider code=server_error" in str(caught.exception),
                                 body.get('error', {}).get('code') == 'server_error')
                self.assertEqual("provider reported overload" in str(caught.exception),
                                 "overloaded" in str(body))
                self.assertIs(caught.exception.__cause__, error)
        async def chunks():
            yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}
        class Stream:
            close = AsyncMock()
            def __aiter__(self):
                return chunks()
        ledger = BudgetLedger({"llm_requests": 3, "total_tokens": 100})
        client = LLMClient(LLMSettings(api_key="fixture", api_mode="chat", stream=True,
            request_timeout_sec=1, max_output_tokens=10, retry_attempts=2), budget_ledger=ledger)
        with patch("openai.AsyncOpenAI") as factory, patch("openai.DefaultAsyncHttpxClient"), \
                patch("simple_ar.integrations.llm.time.sleep") as backoff:
            sdk = factory.return_value
            sdk.close = AsyncMock()
            sdk.chat.completions.create = AsyncMock(side_effect=[TimeoutError("network timed out"), Stream()])
            self.assertEqual(client.ask("system", "user"), "ok")
        self.assertEqual(sdk.chat.completions.create.await_count, 2)
        self.assertEqual(sdk.close.await_count, 2)
        backoff.assert_called_once()
        self.assertEqual([row.status for row in ledger.entries], ["unknown", "settled"])

        # SDK wrappers can hide the phase in __cause__. A proven connection
        # failure has no generated tokens; read/write/opaque failures do not.
        from openai import APIConnectionError
        request = httpx.Request('POST', 'https://invalid.test')
        for cause_type in (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
                           httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError, None):
            with self.subTest(transport_cause=cause_type):
                error = APIConnectionError(request=request)
                if cause_type:
                    error.__cause__ = cause_type('secret-fixture', request=request)
                ledger = BudgetLedger({'llm_requests': 3, 'total_tokens': 100})
                client = LLMClient(LLMSettings(api_key='fixture', api_mode='chat', stream=True,
                    retry_attempts=2), budget_ledger=ledger)
                with patch('simple_ar.integrations.llm._call_provider', side_effect=[error,
                        {'choices': [{'message': {'content': 'ok'}}],
                         'usage': {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}}]) as send, \
                        patch('simple_ar.integrations.llm.time.sleep') as backoff:
                    if cause_type in (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                        self.assertEqual(client.ask('system', 'user'), 'ok')
                        self.assertEqual(send.call_count, 2)
                        self.assertEqual(ledger.entries[0].actual['total_tokens'], 0)
                        self.assertEqual(ledger.entries[0].actual_source, 'transport_pre_dispatch')
                        self.assertEqual(ledger.remaining('total_tokens'), 97)
                        backoff.assert_called_once()
                    else:
                        with self.assertRaises(LLMError):
                            client.ask('system', 'user')
                        send.assert_called_once()
                        backoff.assert_not_called()
                        self.assertEqual(ledger.entries[0].status, 'unknown')
                        self.assertIsNone(ledger.remaining('total_tokens'))

    def test_sdk_transport_failures_preserve_safe_observation_and_accounting(self):
        import httpx
        import ssl
        from openai import APIConnectionError, APITimeoutError
        from simple_ar.integrations.llm import _provider_failure_summary, _transport_failure_phase

        # Share the same failure contract across both sync APIs and async Chat.
        # A redirected request can fail TLS after an earlier request was sent.
        failures = (
            (('connection.connect_tcp.failed',), httpx.ConnectError, False),
            (('connection.start_tls.failed',), httpx.ConnectTimeout, False),
            (('http11.send_request_headers.started', 'http11.receive_response_headers.failed'),
             httpx.ReadTimeout, True),
            (('http2.send_request_headers.started', 'connection.start_tls.failed'),
             httpx.ConnectError, True),
        )
        secret = 'secret-trace-fixture'
        for mode, streamed in (('chat', False), ('responses', False), ('chat', True)):
            for events, cause_type, sent in failures:
                with self.subTest(mode=mode, streamed=streamed, events=events), \
                        patch('openai.OpenAI') as sync_factory, \
                        patch('openai.AsyncOpenAI') as async_factory, \
                        patch('openai.DefaultHttpxClient') as sync_transport, \
                        patch('openai.DefaultAsyncHttpxClient') as async_transport:
                    factory = async_factory if streamed else sync_factory
                    transport = async_transport if streamed else sync_transport
                    sdk = factory.return_value
                    request = httpx.Request('POST', 'https://invalid.test/' + secret,
                                            headers={'Authorization': 'Bearer ' + secret})
                    error = (APITimeoutError(request=request) if issubclass(cause_type, httpx.TimeoutException)
                             else APIConnectionError(request=request))
                    cause = cause_type(secret, request=request)
                    error.__cause__ = cause
                    tls_cause = ssl.SSLError(secret) if events[-1] == 'connection.start_tls.failed' else None
                    if tls_cause is not None:
                        cause.__cause__ = tls_cause

                    def observations():
                        # A fresh request hook on redirect must not reset sent.
                        for event in (*events, secret):
                            yield SimpleNamespace(extensions={}), event

                    def fail(**kwargs):
                        for traced, event in observations():
                            transport.call_args.kwargs['event_hooks']['request'][0](traced)
                            traced.extensions['trace'](event, {'request': request, 'secret': secret})
                        raise error

                    async def fail_async(**kwargs):
                        for traced, event in observations():
                            await transport.call_args.kwargs['event_hooks']['request'][0](traced)
                            await traced.extensions['trace'](event, {'request': request, 'secret': secret})
                        raise error

                    create = sdk.responses.create if mode == 'responses' else sdk.chat.completions.create
                    if streamed:
                        sdk.chat.completions.create = create = AsyncMock(side_effect=fail_async)
                        sdk.close = AsyncMock()
                    else:
                        create.side_effect = fail
                    ledger = BudgetLedger({'llm_requests': 1, 'total_tokens': 100})
                    client = LLMClient(LLMSettings(api_key='fixture', api_mode=mode, stream=streamed,
                        request_timeout_sec=1, max_output_tokens=10, retry_attempts=1,
                        connect_retry_attempts=2 if sent else 1), budget_ledger=ledger)
                    with patch('simple_ar.integrations.llm.time.sleep') as backoff:
                        with self.assertRaises(LLMError) as caught:
                            client.ask('system', 'user')

                    phase = 'possibly_dispatched' if sent else 'not_dispatched'
                    self.assertIs(caught.exception.__cause__, error)
                    self.assertIs(error.__cause__, cause)
                    self.assertEqual(error._simple_ar_transport_event, events[-1])
                    self.assertIs(error._simple_ar_request_started, sent)
                    self.assertEqual(_transport_failure_phase(error), phase)
                    summary = _provider_failure_summary(error)
                    self.assertIn(cause_type.__name__, summary)
                    if tls_cause is not None:
                        self.assertIs(cause.__cause__, tls_cause)
                        self.assertIn(cause_type.__name__ + '>SSLError', summary)
                    self.assertIn('transport event=' + events[-1], summary)
                    self.assertIn('transport phase=' + phase, summary)
                    for diagnostic in (summary, str(caught.exception), ledger.entries[0].reason):
                        self.assertNotIn(secret, diagnostic)
                        self.assertNotIn('invalid.test', diagnostic)
                    entry, = ledger.entries
                    self.assertEqual(entry.status, 'unknown' if sent else 'settled')
                    self.assertEqual(entry.actual, {'llm_requests': 1} if sent else
                                     {'llm_requests': 1, 'total_tokens': 0})
                    self.assertEqual(ledger.unknown_dimensions(), ('total_tokens',) if sent else ())
                    if not sent:
                        self.assertEqual(entry.actual_source, 'transport_pre_dispatch')
                    else:
                        self.assertIn(cause_type.__name__, entry.reason)
                        self.assertIn(events[-1], entry.reason)
                    factory.assert_called_once()
                    self.assertEqual(factory.call_args.kwargs['max_retries'], 0)
                    self.assertIs(factory.call_args.kwargs['http_client'], transport.return_value)
                    transport.assert_called_once()
                    self.assertEqual(set(transport.call_args.kwargs), {'http2', 'event_hooks'})
                    self.assertIs(transport.call_args.kwargs['http2'], False)
                    create.assert_called_once()
                    sdk.close.assert_called_once()
                    if streamed:
                        create.assert_awaited_once()
                        sdk.close.assert_awaited_once()
                    (sync_factory if streamed else async_factory).assert_not_called()
                    (sync_transport if streamed else async_transport).assert_not_called()
                    backoff.assert_not_called()

    def test_sdk_stream_loop_preflight_and_synchronous_paths_unchanged(self):
        ledger = BudgetLedger({"llm_requests": 1})
        client = LLMClient(LLMSettings(api_key="fixture", api_mode="chat", stream=True,
                                     request_timeout_sec=1), budget_ledger=ledger)
        async def inside_loop():
            with self.assertRaisesRegex(LLMError, "asyncio.to_thread"):
                client.ask("system", "user")
            with self.assertRaisesRegex(LLMError, "asyncio.to_thread"):
                _call_openai_sdk("chat", {"api_key": "fixture", "stream": True, "timeout": 1})
            # Neither synchronous non-streamed API is routed through asyncio.run.
            for mode in ("chat", "responses"):
                _call_openai_sdk(mode, {"api_key": "fixture", "model": "fixture", "timeout": 1})
        with patch("openai.AsyncOpenAI") as async_factory, patch("openai.OpenAI") as sync_factory, \
                patch("openai.DefaultHttpxClient"):
            asyncio.run(inside_loop())
        self.assertEqual(ledger.entries, ())
        async_factory.assert_not_called()
        self.assertEqual(sync_factory.call_count, 2)
        self.assertEqual(sync_factory.call_args.kwargs["max_retries"], 0)

    def test_json_mode_defaults_auto_and_respects_explicit_compatibility_choice(self):
        from simple_ar.integrations.llm import _json_response_format_mode
        self.assertEqual(LLMSettings().json_response_format, "auto")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_json_response_format_mode("SIMPLE_AR_JSON_RESPONSE_FORMAT"), "auto")
        with patch.dict(os.environ, {"SIMPLE_AR_JSON_RESPONSE_FORMAT": "off"}):
            self.assertEqual(_json_response_format_mode("SIMPLE_AR_JSON_RESPONSE_FORMAT"), "off")

    def test_auto_json_only_falls_back_for_explicit_format_rejection(self):
        client = LLMClient(LLMSettings(api_key="fixture", api_mode="chat"))
        with patch.object(client, "ask", side_effect=[LLMError("response_format json_object is not supported"), '{"ready":true}']) as ask:
            self.assertEqual(client.ask_json("system", "user"), {"ready": True})
            self.assertEqual(ask.call_count, 2)
            self.assertEqual(ask.call_args_list[0].kwargs["response_format"], {"type": "json_object"})
            self.assertNotIn("response_format", ask.call_args_list[1].kwargs)

    def test_json_format_failure_is_distinct_from_provider_failure(self):
        client = LLMClient(LLMSettings(api_key="test-key", api_mode="chat"))
        with patch.object(client, "ask", return_value="not JSON"):
            with self.assertRaises(LLMResponseError):
                client.ask_json("system", "user")
        failure = LLMError("provider timed out")
        with patch.object(client, "ask", side_effect=failure) as ask:
            with self.assertRaises(LLMError) as caught:
                client.ask_json("system", "user")
            self.assertIs(caught.exception, failure)
            self.assertEqual(ask.call_count, 1)

    def test_usage_recording_keeps_batch_projection_and_unknown_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "meta"
            batch = Path(tmp) / "batch"
            messages = []
            record_usage(meta, LLMUsage("fixture", "plan", 10, 2, 12, "provider", 0.01),
                         stage="code_task.plan", message_callback=messages.append)
            record_usage(meta, LLMUsage("fixture", "edit", 20, 3, 23, "provider", provider_attempts=2),
                         stage="code_task.propose_edits", batch_dir=batch,
                         message_callback=messages.append)
            summary = read_json(meta / "llm_usage_summary.json")
            self.assertEqual(summary["requests"], 2)
            self.assertEqual(summary["total_tokens"], 35)
            self.assertEqual(summary["retry_count"], 1)
            self.assertIsNone(summary["estimated_cost_usd"])
            self.assertEqual(read_json(batch / "usage_summary.json")["total_tokens"], 23)
            self.assertEqual(read_jsonl(batch / "usage.jsonl"), read_jsonl(meta / "llm_usage.jsonl")[1:])
            self.assertIn("$0.010000", messages[0])
            self.assertNotIn("est cost", messages[1])

    def test_usage_summary_keeps_legacy_rows_and_counts_provider_retries(self) -> None:
        summary = summarize_usage(
            [
                {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
                {
                    "prompt_tokens": 4,
                    "completion_tokens": 1,
                    "total_tokens": 5,
                    "provider_attempts": 3,
                },
            ]
        )

        self.assertEqual(summary["requests"], 2)
        self.assertEqual(summary["provider_attempts"], 4)
        self.assertEqual(summary["retry_count"], 2)

    def test_parse_direct_json_object(self) -> None:
        self.assertEqual(parse_json_object('{"a": 1}'), {"a": 1})

    def test_parse_fenced_json_object(self) -> None:
        text = 'Here:\n```json\n{"a": 1, "b": "x"}\n```'
        self.assertEqual(parse_json_object(text), {"a": 1, "b": "x"})

    def test_parse_embedded_json_object(self) -> None:
        text = 'prefix {"a": {"nested": true}} suffix'
        self.assertEqual(parse_json_object(text), {"a": {"nested": True}})

    def test_reject_non_object_json(self) -> None:
        self.assertIsNone(parse_json_object("[1, 2, 3]"))

    def test_accept_single_object_array_from_compatible_gateway(self) -> None:
        self.assertEqual(parse_json_object('[{"a": 1}]'), {"a": 1})

    def test_literal_unknown_string_escapes_preserve_scientific_and_path_text(self) -> None:
        raw = r'{"math":"\(p\\lesssim0.02\)","path":"C:\work\q","nested":{"value":3}}'
        expected = {"math": r"\(p\lesssim0.02\)", "path": r"C:\work\q", "nested": {"value": 3}}
        for wrapper in (raw, "```json\n" + raw + "\n```", "Result:\n" + raw + "\nEnd."):
            with self.subTest(wrapper=wrapper):
                self.assertEqual(parse_json_object(wrapper), expected)
        self.assertIn(r"\(", raw)

    def test_valid_escapes_keep_the_standard_decoder_meaning(self) -> None:
        raw = r'{"text":"line\n\t\/\"\\","unicode":"\u00e9","braces":"{text}"}'
        self.assertEqual(parse_json_object(raw), json.loads(raw))
        self.assertEqual(parse_json_object("prefix " + raw + " suffix"), json.loads(raw))

    def test_damaged_outer_json_never_salvages_a_nested_record(self) -> None:
        for raw in (
            '{"broken":, "nested":{"looks_valid":true}}',
            '{"unclosed":{"looks_valid":true}',
            '{"broken":"unterminated, "nested":{"looks_valid":true}}',
            '[{"looks_valid":true},',
            'prefix [{"looks_valid":true}, suffix',
            '{"broken":,} {"looks_valid":true}',
            '```json\n{"broken":, "nested":{"looks_valid":true}}\n```',
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_json_object(raw))

    def test_unknown_escape_recovery_does_not_guess_unicode_controls_or_delimiters(self) -> None:
        for raw in (
            r'{"text":"\uNOTHEX","nested":{"valid":true}}',
            r'{"text":"\q\uNOTHEX","nested":{"valid":true}}',
            '{"text":"\\q\nunescaped newline","nested":{"valid":true}}',
            r'{"text":"\q","nested":{"valid":true}',
            r'{\q"nested":{"valid":true}}',
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_json_object(raw))

    def test_ask_json_literal_escape_recovery_does_not_send_another_request(self) -> None:
        client = LLMClient(LLMSettings(api_key="test-key", api_mode="chat"))
        raw = r'{"draft_markdown":"\(x\\in R\)","nested":{"value":4}}'
        response = {"choices": [{"message": {"content": raw}}]}
        with patch("simple_ar.integrations.llm._call_openai_sdk", return_value=response) as call:
            self.assertEqual(client.ask_json("system", "user"),
                {"draft_markdown": r"\(x\in R\)", "nested": {"value": 4}})
        self.assertEqual(call.call_count, 1)
        self.assertEqual(response["choices"][0]["message"]["content"], raw)
        with patch.object(client, 'ask', return_value='{"edits": [broken code response') as call:
            with self.assertRaises(LLMResponseError) as failure:
                client.ask_json('system', 'user')
            self.assertEqual(failure.exception.raw_response, '{"edits": [broken code response')
            self.assertNotIn('broken code response', str(failure.exception))
            self.assertEqual(call.call_count, 1)  # Format recovery belongs to the task, not transport.

    def test_ask_json_reads_chat_content_blocks(self) -> None:
        client = LLMClient(LLMSettings(api_key="test-key", api_mode="chat"))
        response = {
            "choices": [
                {
                    "message": {
                        "content": [{"type": "text", "text": '{"ok": true}'}],
                    }
                }
            ]
        }

        with patch("simple_ar.integrations.llm._call_openai_sdk", return_value=response):
            self.assertEqual(client.ask_json("system", "user"), {"ok": True})

    def test_streamed_chat_chunks_are_assembled_and_usage_is_settled(self) -> None:
        client = LLMClient(LLMSettings(api_key="test-key", api_mode="chat", stream=True))
        chunks = iter([
            {"choices": [{"delta": {"content": "hel"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}},
        ])
        with patch("simple_ar.integrations.llm._call_openai_sdk", return_value=chunks) as call:
            self.assertEqual(client.ask("system", "user", label="stream-test"), "hello")

        request = call.call_args.args[1]
        self.assertTrue(request["stream"])
        self.assertEqual(request["extra_headers"]["Accept-Encoding"], "identity")

        # Same transport contract for native history: multiple fragmented calls,
        # thinking round-trip, tool-only output and accounting without a tail.
        tools = [{"type": "function", "function": {"name": "read_file",
                  "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
        history = [{"role": "system", "content": "inspect"},
                   {"role": "user", "content": "read relevant sources"}]
        observed = []
        ledger = BudgetLedger({"llm_requests": 4, "total_tokens": 10000})
        native = LLMClient(LLMSettings(api_key="test-key", api_mode="chat", stream=True),
                           usage_callback=observed.append, budget_ledger=ledger)
        fragments = [
            {"choices": [{"delta": {"reasoning_content": "inspect first", "tool_calls": [
                {"index": 1, "id": "second", "function": {"name": "read_file", "arguments": '{"path":'}},
                {"index": 0, "id": "first", "function": {"name": "read_file", "arguments": '{"path":'}}]},
                "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '"README.md"}'}},
                {"index": 1, "function": {"arguments": '"src/main.py"}'}}]},
                "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60}},
        ]
        with patch("simple_ar.integrations.llm._call_openai_sdk", return_value=iter(fragments)):
            message = native.chat_turn(history, tools=tools, max_output_tokens=500)
        self.assertEqual(message["reasoning_content"], "inspect first")
        self.assertEqual([row["id"] for row in message["tool_calls"]], ["first", "second"])
        self.assertEqual(json.loads(message["tool_calls"][1]["function"]["arguments"]), {"path": "src/main.py"})
        history += [message, {"role": "tool", "tool_call_id": "first", "content": "readme"},
                    {"role": "tool", "tool_call_id": "second", "content": "source"}]
        with patch("simple_ar.integrations.llm._call_openai_sdk", return_value=iter([
            {"choices": [{"delta": {"content": "done", "reasoning_content": ""}, "finish_reason": "stop"}]},
        ])) as call:
            final = native.chat_turn(history, tools=tools, max_output_tokens=500)
        self.assertEqual(final["content"], "done")
        self.assertIn("reasoning_content", final)
        self.assertEqual(call.call_args.args[1]["messages"], history)
        self.assertEqual(observed[0].total_tokens, 60)
        self.assertEqual(observed[1].source, "estimated")
        for reason, delta in (("length", {"tool_calls": [
            {"index": 0, "id": "cut", "function": {"name": "read_file", "arguments": '{"path":'}}]}),
            ("stop", {"reasoning_content": "no answer"})):
            with patch("simple_ar.integrations.llm._call_openai_sdk", return_value=iter([
                {"choices": [{"delta": delta, "finish_reason": reason}]}
            ])), self.assertRaises(LLMResponseError):
                native.chat_turn(history, tools=tools, max_output_tokens=500)
        wrong_mode = LLMClient(LLMSettings(api_key="test-key", api_mode="responses"))
        with patch("simple_ar.integrations.llm._call_openai_sdk") as call, self.assertRaises(LLMError):
            wrong_mode.chat_turn(history, tools=tools)
        call.assert_not_called()

    def test_completed_stream_with_final_usage_does_not_wait_for_transport_eof(self):
        observed = []
        client = LLMClient(LLMSettings(api_key="test-key", api_mode="chat", stream=True),
                           usage_callback=observed.append)

        class CompletedStream:
            closed = False

            def __iter__(self):
                yield {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}
                yield {"choices": [{"delta": {"content": "complete"}, "finish_reason": "stop"}]}
                yield {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
                raise AssertionError("Do not wait for another event after terminal content and usage")

            def close(self):
                self.closed = True

        stream = CompletedStream()
        with patch("simple_ar.integrations.llm._call_openai_sdk", return_value=stream):
            self.assertEqual(client.ask("system", "user"), "complete")
        self.assertTrue(stream.closed)
        self.assertEqual(observed[0].total_tokens, 5)
        with patch("simple_ar.integrations.llm._call_openai_sdk", return_value=iter([
            {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}},
            {"choices": [{"delta": {"content": "complete"}, "finish_reason": "stop"}]},
        ])):
            self.assertEqual(client.ask("system", "user"), "complete")
        self.assertEqual(observed[-1].total_tokens, 4)

    def test_from_env_reads_stream_setting(self) -> None:
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-key",
            "SIMPLE_AR_LLM_API": "chat",
            "SIMPLE_AR_LLM_STREAM": "true",
        }, clear=True):
            client = LLMClient.from_env()

        self.assertTrue(client._settings.stream)

    def test_clean_eof_without_completion_is_not_adopted(self):
        ledger = BudgetLedger({"llm_requests": 1})
        client = LLMClient(LLMSettings(api_key="test", api_mode="chat", stream=True,
                                      retry_attempts=1), budget_ledger=ledger)
        with patch("simple_ar.integrations.llm._call_openai_sdk", return_value=iter([
            {"choices": [{"delta": {"content": '{"looks_complete": true}'}}]},
        ])):
            with self.assertRaisesRegex(LLMError, "without a completion marker"):
                client.ask("system", "user")
        self.assertEqual(ledger.entries[0].status, "unknown")

    def test_finished_generation_with_failed_accounting_tail_is_not_retried(self):
        observed = []
        client = LLMClient(LLMSettings(api_key="test", api_mode="chat", stream=True,
                                      retry_attempts=3), usage_callback=observed.append)
        def stream():
            yield {"choices": [{"delta": {"content": "complete"}, "finish_reason": "stop"}]}
            raise TimeoutError("accounting tail did not arrive")
        with patch("simple_ar.integrations.llm._call_openai_sdk", return_value=stream()) as call:
            self.assertEqual(client.ask("system", "user"), "complete")
        self.assertEqual(call.call_count, 1)
        self.assertEqual(observed[0].source, "estimated")

    def test_finished_generation_does_not_wait_for_a_live_accounting_tail(self):
        import threading
        observed = []
        closed = threading.Event()
        class Stream:
            def __iter__(self):
                yield {"choices": [{"delta": {"content": "complete"}, "finish_reason": "stop"}]}
                closed.wait(10)
            def close(self):
                closed.set()
        client = LLMClient(LLMSettings(api_key="test", api_mode="chat", stream=True,
                                      retry_attempts=1), usage_callback=observed.append)
        started = time.monotonic()
        with patch("simple_ar.integrations.llm._call_openai_sdk", return_value=Stream()):
            self.assertEqual(client.ask("system", "user"), "complete")
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(closed.is_set())
        self.assertEqual(observed[0].source, "estimated")

    def test_cancelled_provider_request_does_not_remain_inflight_or_retry(self):
        ledger = BudgetLedger({"llm_requests": 3})
        client = LLMClient(LLMSettings(api_key="test", api_mode="chat", retry_attempts=3),
                           budget_ledger=ledger)
        with patch("simple_ar.integrations.llm._call_openai_sdk", side_effect=KeyboardInterrupt) as call:
            with self.assertRaises(KeyboardInterrupt):
                client.ask("system", "user")
        self.assertEqual(call.call_count, 1)
        self.assertEqual(ledger.entries[0].status, "unknown")

    def test_sdk_client_lives_until_stream_consumed_and_closes_on_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail), patch("openai.OpenAI") as factory:
                client = factory.return_value
                def chunks():
                    client.close.assert_not_called()
                    if fail:
                        raise KeyboardInterrupt()
                    yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}
                client.chat.completions.create.return_value = chunks()
                request = {"api_key": "test", "model": "test", "stream": True, "messages": []}
                if fail:
                    with self.assertRaises(KeyboardInterrupt):
                        _call_openai_sdk("chat", request)
                else:
                    response = _call_openai_sdk("chat", request)
                    self.assertEqual(response["choices"][0]["message"]["content"], "ok")
                client.close.assert_called_once()

    def test_optional_chat_thinking_switch_reaches_provider(self) -> None:
        response = {"choices": [{"message": {"content": "ok"}}]}
        for effort in ("", "none"):
            with self.subTest(effort=effort):
                client = LLMClient(LLMSettings(api_key="test-key", api_mode="chat",
                    thinking_mode="disabled", reasoning_effort=effort))
                with patch("simple_ar.integrations.llm._call_openai_sdk", return_value=response) as call:
                    self.assertEqual(client.ask("system", "user"), "ok")
                expected = {"thinking": {"type": "disabled"}}
                if effort:
                    expected["reasoning_effort"] = effort
                self.assertEqual(call.call_args.args[1]["extra_body"], expected)

    def test_thinking_switch_rejects_incompatible_api_or_effort(self) -> None:
        cases = [("responses", thinking, effort) for thinking in ("disabled", "enabled", "auto")
                 for effort in ("", "none")]
        cases += [("chat", "disabled", effort) for effort in
                  ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")]
        for mode, thinking, effort in cases:
            with self.subTest(mode=mode, thinking=thinking, effort=effort):
                client = LLMClient(LLMSettings(api_key="test-key", api_mode=mode,
                    thinking_mode=thinking, reasoning_effort=effort, reasoning_output_tokens=8192))
                if thinking == "disabled":
                    self.assertIsNone(client.effective_output_limit())
                    self.assertEqual(client.effective_output_limit(1200), 1200)
                with patch("simple_ar.integrations.llm._call_openai_sdk") as provider:
                    with self.assertRaises(LLMError):
                        client.ask("system", "user")
                provider.assert_not_called()

    def test_from_env_reads_optional_thinking_switch(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "SIMPLE_AR_LLM_THINKING": "disabled"}, clear=True):
            self.assertEqual(LLMClient.from_env()._settings.thinking_mode, "disabled")

    def test_ask_json_many_preserves_input_order(self) -> None:
        client = object.__new__(LLMClient)

        def fake_ask_json(system: str, user: str, *, label: str = "") -> dict[str, str]:
            return {"system": system, "user": user}

        client.ask_json = fake_ask_json
        requests = [
            LLMRequest(system="s", user="first", label="a"),
            LLMRequest(system="s", user="second", label="b"),
            LLMRequest(system="s", user="third", label="c"),
        ]

        results = LLMClient.ask_json_many(client, requests, max_workers=2)

        self.assertEqual([item["user"] for item in results], ["first", "second", "third"])

    def test_ask_many_rejects_invalid_worker_count(self) -> None:
        client = object.__new__(LLMClient)
        requests = [LLMRequest(system="s", user="u")]

        with self.assertRaises(LLMError):
            LLMClient.ask_many(client, requests, max_workers=0)

    def test_provider_worker_cap_applies_without_changing_batch_order(self) -> None:
        client = LLMClient(LLMSettings(api_key="test-key"))
        requests = [LLMRequest("s", str(index)) for index in range(3)]
        with patch.dict(os.environ, {"SIMPLE_AR_LLM_MAX_WORKERS": "1"}), patch.object(
            client, "ask_json", side_effect=lambda system, user, label="": {"index": int(user)}
        ), patch("simple_ar.integrations.llm.ThreadPoolExecutor", wraps=ThreadPoolExecutor) as pool:
            result = client.ask_json_many(requests, max_workers=3)
        self.assertEqual([row["index"] for row in result], [0, 1, 2])
        self.assertEqual(pool.call_args.kwargs["max_workers"], 1)

    def test_failed_batch_does_not_start_unsent_requests(self) -> None:
        client = LLMClient(LLMSettings(api_key="test-key"))
        calls: list[str] = []

        def fail_first(system: str, user: str, *, label: str = "") -> dict[str, str]:
            calls.append(user)
            raise LLMError("rate limited")

        requests = [LLMRequest("s", str(index), label=f"item-{index}") for index in range(3)]
        with patch.dict(os.environ, {"SIMPLE_AR_LLM_MAX_WORKERS": "1"}), patch.object(
            client, "ask_json", side_effect=fail_first
        ), self.assertRaisesRegex(LLMError, "item-0"):
            client.ask_json_many(requests, max_workers=3)
        self.assertEqual(calls, ["0"])

    def test_screening_worker_count_reflects_provider_cap(self) -> None:
        from simple_ar.research.evidence.screening import _screening_workers

        with patch.dict(os.environ, {"SIMPLE_AR_LLM_MAX_WORKERS": "1"}):
            self.assertEqual(_screening_workers({"llm_max_workers": 8, "read_screening_workers": 3}), 1)

    def test_from_env_configures_provider_timeout(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "test-key",
                "OPENAI_BASE_URL": "https://example.test/v1",
                "SIMPLE_AR_MODEL": "test-model",
                "SIMPLE_AR_LLM_TIMEOUT_SEC": "42.5",
                "SIMPLE_AR_MAX_OUTPUT_TOKENS": "1234",
                "SIMPLE_AR_LLM_RETRY_ATTEMPTS": "5",
                "SIMPLE_AR_LLM_RETRY_BASE_DELAY_SEC": "0.5",
                "SIMPLE_AR_LLM_RETRY_MAX_DELAY_SEC": "9",
                "SIMPLE_AR_LLM_REASONING_EFFORT": "low",
                "SIMPLE_AR_LLM_REASONING_OUTPUT_TOKENS": "8192",
            },
            clear=True,
        ), patch("simple_ar.integrations.llm._call_openai_sdk") as openai_call:
            client = LLMClient.from_env()

        self.assertEqual(client.model, "test-model")
        self.assertEqual(client._settings.transport_backend, "openai")
        self.assertEqual(client._openai_model, "test-model")
        self.assertEqual(client._provider_model, "openai/test-model")
        self.assertEqual(client._settings.request_timeout_sec, 42.5)
        self.assertEqual(client._settings.max_output_tokens, 1234)
        self.assertEqual(client._settings.retry_attempts, 5)
        self.assertEqual(client._settings.retry_base_delay_sec, 0.5)
        self.assertEqual(client._settings.retry_max_delay_sec, 9)
        self.assertEqual(client._settings.reasoning_effort, "low")
        self.assertEqual(client._settings.reasoning_output_tokens, 8192)
        openai_call.assert_not_called()

    def test_effective_output_limit_matches_transport_and_budget_reservation(self) -> None:
        # One table owns precedence, uncapped behavior and API token-field mapping.
        cases = (
            ('native', 'chat', 'glm-5.3-flash', 1200, 2000, 'low', '', 1200, 'max_tokens'),
            ('ask', 'chat', 'gpt-5.1', 80, 1000, 'low', '', 80, 'max_completion_tokens'),
            ('ask', 'responses', 'gpt-5.1', None, 2000, 'low', '', 2000, 'max_output_tokens'),
            ('ask', 'chat', 'glm-5.3-flash', None, None, 'low', '', 8192, 'max_tokens'),
            ('ask', 'responses', 'gpt-5.1', None, None, '', '', None, None),
            ('ask', 'chat', 'gpt-5.1', None, None, 'none', '', None, None),
            ('ask', 'responses', 'gpt-5.1', None, None, 'none', '', None, None),
            ('native', 'chat', 'glm-5.3-flash', None, None, '', 'disabled', None, None),
            ('ask', 'chat', 'gpt-5.1', None, None, 'none', 'disabled', None, None),
            ('ask', 'chat', 'gpt-5.1', None, 2000, 'none', '', 2000, 'max_completion_tokens'),
            ('ask', 'chat', 'gpt-5.1', 80, 1000, 'none', '', 80, 'max_completion_tokens'),
            ('ask', 'chat', 'glm-5.3-flash', None, 2000, '', 'disabled', 2000, 'max_tokens'),
            ('native', 'chat', 'glm-5.3-flash', 1200, 2000, 'none', 'disabled', 1200, 'max_tokens'),
        )
        response = {'choices': [{'message': {'content': 'ok'}}],
            'usage': {'prompt_tokens': 4, 'completion_tokens': 1, 'total_tokens': 5}}
        for entry, mode, model, override, configured, effort, thinking, expected, field in cases:
            with self.subTest(entry=entry, mode=mode, override=override, configured=configured,
                              effort=effort, thinking=thinking):
                ledger = BudgetLedger({'total_tokens': 20000, 'llm_requests': 1})
                client = LLMClient(LLMSettings(api_key='test-key', model=model, api_mode=mode,
                    max_output_tokens=configured, reasoning_effort=effort, thinking_mode=thinking,
                    reasoning_output_tokens=8192),
                    budget_ledger=ledger)
                settings_before = client.connection_binding()
                self.assertEqual(client.effective_output_limit(override), expected)
                with patch('simple_ar.integrations.llm._call_openai_sdk', return_value=response) as provider:
                    if entry == 'native':
                        messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'user'}]
                        self.assertEqual(client.chat_turn(messages, tools=[], max_output_tokens=override)['content'], 'ok')
                        prompt_cost = estimate_tokens('[]') + estimate_tokens(json.dumps(messages, ensure_ascii=False))
                    else:
                        self.assertEqual(client.ask('system', 'user', max_output_tokens=override), 'ok')
                        prompt_cost = estimate_tokens('system') + estimate_tokens('user')
                provider.assert_called_once()
                actual_mode, request = provider.call_args.args
                self.assertEqual(actual_mode, mode)
                self.assertEqual(request['model'], model)
                self.assertEqual({key: request[key] for key in
                    ('max_tokens', 'max_completion_tokens', 'max_output_tokens') if key in request},
                    {} if field is None else {field: expected})
                if mode == 'chat' and (effort or thinking):
                    expected_options = {'reasoning_effort': effort} if effort else {}
                    if thinking:
                        expected_options['thinking'] = {'type': thinking}
                    self.assertEqual(request['extra_body'], expected_options)
                self.assertEqual(ledger.entries[0].reserved['total_tokens'],
                    prompt_cost + (expected or 0))
                self.assertEqual(ledger.entries[0].actual['total_tokens'], 5)
                self.assertEqual(client.connection_binding(), settings_before)

    def test_reasoning_option_is_added_only_when_chat_mode_is_selected(self) -> None:
        request = {
            "model": "glm-5.3-flash",
            "instructions": "system",
            "input": [{"role": "user", "content": "user"}],
            "api_key": "test-key",
        }
        responses_request = _request_for_api_mode(
            request, "responses", reasoning_effort="low"
        )
        responses_stream_request = _request_for_api_mode(
            request, "responses", reasoning_effort="low", stream=True
        )
        chat_request = _request_for_api_mode(
            request, "chat", reasoning_effort="low"
        )

        self.assertNotIn("extra_body", responses_request)
        self.assertNotIn("stream", responses_stream_request)
        self.assertEqual(chat_request["extra_body"], {"reasoning_effort": "low"})

    def test_empty_reasoning_only_response_has_actionable_error(self) -> None:
        client = LLMClient(LLMSettings(api_key="test-key", api_mode="chat"))
        response = {
            "choices": [{
                "finish_reason": "length",
                "message": {
                    "content": "",
                    "reasoning_content": "internal reasoning",
                },
            }]
        }
        with patch("simple_ar.integrations.llm._call_openai_sdk", return_value=response):
            with self.assertRaisesRegex(LLMResponseError, "reasoning content"):
                client.ask("system", "user")

    def test_length_response_without_reasoning_has_output_cap_hint(self) -> None:
        client = LLMClient(LLMSettings(api_key="test-key", api_mode="chat"))
        response = {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
        with patch("simple_ar.integrations.llm._call_openai_sdk", return_value=response):
            with self.assertRaisesRegex(LLMResponseError, "Increase the per-call output token cap"):
                client.ask("system", "user")

    def test_ask_retries_transient_connection_error(self) -> None:
        usage: list[object] = []
        client = LLMClient(
            LLMSettings(
                api_key="test-key",
                api_mode="chat",
                transport_backend="litellm",
                retry_attempts=3,
                retry_base_delay_sec=0.25,
                retry_max_delay_sec=2.0,
            ),
            usage_callback=usage.append,
        )
        response = {"choices": [{"message": {"content": "ok"}}]}

        with patch.dict('sys.modules', {'litellm': Mock()}), patch(
            "litellm.completion",
            side_effect=[RuntimeError("Connection error."), response],
        ) as completion, patch("simple_ar.integrations.llm.time.sleep") as sleep:
            output = client.ask("system", "user", label="retry-test")

        self.assertEqual(output, "ok")
        self.assertEqual(completion.call_count, 2)
        sleep.assert_called_once_with(0.25)
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0].provider_attempts, 2)

    def test_ask_retries_timeout_and_cloudflare_524_with_capped_backoff(self) -> None:
        usage: list[object] = []
        client = LLMClient(
            LLMSettings(
                api_key="test-key",
                api_mode="chat",
                transport_backend="litellm",
                retry_attempts=3,
                retry_base_delay_sec=0.25,
                retry_max_delay_sec=0.5,
            ),
            usage_callback=usage.append,
        )
        response = {"choices": [{"message": {"content": "ok"}}]}

        with patch.dict('sys.modules', {'litellm': Mock()}), patch(
            "litellm.completion",
            side_effect=[
                RuntimeError("Request timed out."),
                RuntimeError("Cloudflare 524 Origin Time-out."),
                response,
            ],
        ) as completion, patch("simple_ar.integrations.llm.time.sleep") as sleep:
            output = client.ask("system", "user", label="timeout-524-retry")

        self.assertEqual(output, "ok")
        self.assertEqual(completion.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.25, 0.5])
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0].provider_attempts, 3)

    def test_auto_transport_error_falls_back_to_chat(self) -> None:
        usage: list[object] = []
        client = LLMClient(
            LLMSettings(
                api_key="test-key",
                api_mode="auto",
                transport_backend="litellm",
                retry_attempts=2,
                retry_base_delay_sec=0.25,
                retry_max_delay_sec=2.0,
                max_output_tokens=2000,
                reasoning_effort='low',
                reasoning_output_tokens=8192,
            ),
            usage_callback=usage.append,
        )
        response = {"choices": [{"message": {"content": "ok"}}]}

        with patch.dict('sys.modules', {'litellm': Mock()}), patch(
            "litellm.responses",
            side_effect=RuntimeError("Server disconnected without sending a response."),
        ) as responses, patch(
            "litellm.completion",
            return_value=response,
        ) as completion, patch("simple_ar.integrations.llm.time.sleep") as sleep:
            output = client.ask("system", "user", max_output_tokens=1200, label="compat-test")

        self.assertEqual(output, "ok")
        self.assertEqual(responses.call_count, 1)
        self.assertEqual(completion.call_count, 1)
        completion_request = completion.call_args.kwargs
        self.assertEqual(client.effective_output_limit(1200), 1200)
        self.assertEqual(responses.call_args.kwargs['max_output_tokens'], 1200)
        chat_caps = {key: completion_request[key] for key in
            ('max_tokens', 'max_completion_tokens') if key in completion_request}
        self.assertEqual(len(chat_caps), 1)
        self.assertEqual(list(chat_caps.values()), [1200])
        self.assertNotIn('max_output_tokens', completion_request)
        self.assertEqual(completion_request['model'], responses.call_args.kwargs['model'])
        self.assertEqual(
            completion_request["messages"],
            [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "user"},
            ],
        )
        sleep.assert_not_called()
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0].provider_attempts, 2)

    def test_responses_transport_error_stays_on_responses(self) -> None:
        client = LLMClient(
            LLMSettings(
                api_key="test-key",
                api_mode="responses",
                transport_backend="litellm",
                retry_attempts=2,
                retry_base_delay_sec=0.25,
                retry_max_delay_sec=2.0,
            )
        )

        with patch.dict('sys.modules', {'litellm': Mock()}), patch(
            "litellm.responses",
            side_effect=RuntimeError("503 Service Unavailable"),
        ) as responses, patch(
            "litellm.completion"
        ) as completion, patch("simple_ar.integrations.llm.time.sleep") as sleep:
            with self.assertRaises(LLMError):
                client.ask("system", "user", label="strict-responses-test")

        self.assertEqual(responses.call_count, 2)
        completion.assert_not_called()
        sleep.assert_called_once_with(0.25)

    def test_responses_disconnect_is_retried_in_strict_mode(self) -> None:
        client = LLMClient(
            LLMSettings(
                api_key="test-key",
                api_mode="responses",
                transport_backend="litellm",
                retry_attempts=2,
                retry_base_delay_sec=0.25,
                retry_max_delay_sec=2.0,
            )
        )

        with patch.dict('sys.modules', {'litellm': Mock()}), patch(
            "litellm.responses",
            side_effect=RuntimeError("Server disconnected without sending a response."),
        ) as responses, patch(
            "litellm.completion"
        ) as completion, patch("simple_ar.integrations.llm.time.sleep") as sleep:
            with self.assertRaises(LLMError):
                client.ask("system", "user", label="strict-disconnect-test")

        self.assertEqual(responses.call_count, 2)
        completion.assert_not_called()
        sleep.assert_called_once_with(0.25)

    def test_default_env_uses_bounded_timeout_but_honors_explicit_output_cap(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "test-key",
                "SIMPLE_AR_MODEL": "gpt-5.1",
                "SIMPLE_AR_LLM_API": "chat",
            },
            clear=True,
        ):
            client = LLMClient.from_env()

        self.assertEqual(client._settings.request_timeout_sec, 180.0)
        self.assertIsNone(client._settings.max_output_tokens)

        response = {"choices": [{"message": {"content": "ok"}}]}
        with patch("simple_ar.integrations.llm._call_openai_sdk", return_value=response) as openai_call:
            output = client.ask("system", "user", max_output_tokens=999, label="uncapped-test")

        self.assertEqual(output, "ok")
        api_mode, request = openai_call.call_args.args
        self.assertEqual(api_mode, "chat")
        self.assertEqual(request["timeout"], 180.0)
        self.assertEqual(request["max_completion_tokens"], 999)

    def test_invalid_per_call_output_cap_fails_before_provider_request(self) -> None:
        client = LLMClient(
            LLMSettings(
                api_key="test-key",
                api_mode="chat",
            )
        )

        with patch("simple_ar.integrations.llm._call_openai_sdk") as openai_call:
            for invalid in (0, -1, 'invalid'):
                with self.subTest(cap=invalid):
                    with self.assertRaisesRegex(LLMError, "positive integer"):
                        client.effective_output_limit(invalid)
                    with self.assertRaisesRegex(LLMError, "positive integer"):
                        client.ask("system", "user", max_output_tokens=invalid)

        openai_call.assert_not_called()

    def test_budget_ledger_settles_successful_provider_attempt(self) -> None:
        ledger = BudgetLedger({"llm_requests": 2, "total_tokens": 100})
        client = LLMClient(
            LLMSettings(
                api_key="test-key",
                api_mode="chat",
                max_output_tokens=10,
                retry_attempts=1,
            ),
            budget_ledger=ledger,
            budget_session_id="session-1",
            budget_attempt_id="attempt-1",
        )
        response = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        }

        with patch(
            "simple_ar.integrations.llm._call_openai_sdk", return_value=response
        ) as openai_call:
            self.assertEqual(
                client.ask("system", "user", label="plan", budget_call_id="call-1"),
                "ok",
            )

        openai_call.assert_called_once()
        self.assertEqual(len(ledger.entries), 1)
        entry = ledger.entries[0]
        self.assertEqual(entry.status, "settled")
        self.assertEqual(entry.session_id, "session-1")
        self.assertEqual(entry.attempt_id, "attempt-1")
        self.assertEqual(entry.logical_call_id, "call-1")
        self.assertEqual(entry.purpose, "plan")
        self.assertEqual(entry.actual, {"llm_requests": 1, "total_tokens": 5})
        self.assertEqual(entry.actual_source, "provider")
        self.assertEqual(ledger.remaining("llm_requests"), 1)
        self.assertEqual(ledger.remaining("total_tokens"), 95)

    def test_with_budget_binds_a_client_copy_to_application_ledger(self) -> None:
        source = LLMClient(
            LLMSettings(api_key="test-key", api_mode="chat", max_output_tokens=10),
        )
        ledger = BudgetLedger({"llm_requests": 1, "total_tokens": 50})
        client = source.with_budget(ledger, session_id="session-1")
        response = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        }

        with patch("simple_ar.integrations.llm._call_openai_sdk", return_value=response):
            self.assertEqual(client.ask("system", "user", label="plan"), "ok")

        self.assertIsNot(client, source)
        self.assertEqual(ledger.entries[0].session_id, "session-1")
        self.assertEqual(ledger.entries[0].actual["total_tokens"], 5)

    def test_task_client_preserves_provider_and_budget_with_role_model_override(self) -> None:
        observed, local = [], []
        ledger = BudgetLedger({"llm_requests": 1, "total_tokens": 100})
        source = LLMClient(
            LLMSettings(api_key="test-key", base_url="https://provider.invalid/v1",
                        model="planner", api_mode="chat", max_output_tokens=10),
            usage_callback=observed.append, budget_ledger=ledger,
            budget_session_id="session-1", budget_attempt_id="implement-1",
        )
        client = LLMClient.for_task(client=source, model="reviewer", usage_callback=local.append)
        response = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        }
        with patch("simple_ar.integrations.llm._call_openai_sdk", return_value=response) as transport:
            self.assertEqual(client.ask("system", "user", label="review"), "ok")
        self.assertEqual(source.model, "planner")
        self.assertEqual(client.model, "reviewer")
        self.assertEqual(transport.call_args.args[1]["model"], "reviewer")
        self.assertEqual(observed, local)
        self.assertEqual(len(local), 1)
        self.assertEqual(len(ledger.entries), 1)
        self.assertEqual(ledger.entries[0].attempt_id, "implement-1")
        self.assertEqual(ledger.remaining("llm_requests"), 0)

    def test_task_output_limit_survives_named_professional_routes_without_rebinding(self):
        from simple_ar.integrations.model_profiles import ModelCatalog, ModelProfile
        profile = ModelProfile(api='openai_chat', base_url='https://provider.invalid/v1',
            capabilities=['text', 'code'], model='gpt-6.1-sol', api_key_env='TASK_CAP_TEST_KEY',
            reasoning_effort='medium', max_output_tokens=900)
        catalog = ModelCatalog(profiles={'task': profile, 'writer': profile},
            routes={'default': 'task', 'code': 'writer', 'writing': 'writer'})
        with patch.dict(os.environ, {'TASK_CAP_TEST_KEY': 'test-key'}):
            source = LLMClient(LLMSettings(**profile.text_settings()), model_catalog=catalog, profile_name='task')
            binding = source.connection_binding()
            operational = source.with_output_limit(321)
            for purpose in ('code', 'writing'):
                routed = LLMClient.for_task(client=operational, purpose=purpose)
                self.assertEqual(routed._profile_name, 'writer')
                self.assertEqual(routed.effective_output_limit(), 321)
                self.assertEqual(routed.effective_output_limit(123), 123)  # Section/call cap keeps precedence.
                self.assertEqual(routed.with_budget(BudgetLedger({'llm_requests': 0})).effective_output_limit(), 321)
                self.assertEqual(routed._settings.max_output_tokens, 900)
            self.assertEqual(operational.connection_binding(), binding)
            self.assertEqual(source.connection_binding(), binding)
            self.assertEqual(source.effective_output_limit(), 900)

    def test_budget_exhaustion_prevents_provider_call(self) -> None:
        ledger = BudgetLedger({"llm_requests": 0, "total_tokens": 100})
        client = LLMClient(
            LLMSettings(api_key="test-key", api_mode="chat"),
            budget_ledger=ledger,
        )

        with patch("simple_ar.integrations.llm._call_openai_sdk") as openai_call:
            with self.assertRaises(LLMError) as caught:
                client.ask("system", "user", label="blocked")

        openai_call.assert_not_called()
        self.assertEqual(ledger.entries, ())
        self.assertIsInstance(caught.exception.__cause__, BudgetExceededError)
        self.assertNotIn("preceding provider failure", str(caught.exception))

    def test_retryable_provider_failure_is_counted_before_retry(self) -> None:
        response = {"choices": [{"message": {"content": "ok"}}]}
        for stop in (None, "llm_requests", "total_tokens", "auto_mode"):
            with self.subTest(stop=stop):
                ledger = BudgetLedger({
                    "llm_requests": 1 if stop in {"llm_requests", "auto_mode"} else 3,
                    "total_tokens": (10 + estimate_tokens("system") + estimate_tokens("user")
                                     if stop == "total_tokens" else 100),
                })
                client = LLMClient(LLMSettings(api_key="test-key",
                    api_mode="auto" if stop == "auto_mode" else "chat",
                    max_output_tokens=10, retry_attempts=1 if stop == "auto_mode" else 2,
                    retry_base_delay_sec=0.25), budget_ledger=ledger)
                failure = RuntimeError("503 Service Unavailable secret-fixture")
                failure.status_code = 503
                with patch("simple_ar.integrations.llm._call_openai_sdk",
                           side_effect=[failure, response]) as openai_call, patch(
                           "simple_ar.integrations.llm.time.sleep"):
                    if stop is None:
                        self.assertEqual(client.ask("system", "user", label="retry"), "ok")
                    else:
                        with self.assertRaises(LLMError) as caught:
                            client.ask("system", "user", label="retry")
                        self.assertIsInstance(caught.exception.__cause__, BudgetExceededError)
                        diagnostic = "".join(traceback.format_exception(caught.exception))
                        self.assertIn("preceding provider failure after 1 physical attempt(s)", diagnostic)
                        self.assertIn("RuntimeError; HTTP status=503", diagnostic)
                        self.assertNotIn("secret-fixture", diagnostic)
                        self.assertIn("total_tokens" if stop == "total_tokens" else "llm_requests",
                                      str(caught.exception.__cause__))
                self.assertEqual(openai_call.call_count, 2 if stop is None else 1)
                self.assertEqual([entry.status for entry in ledger.entries],
                                 ["settled", "settled"] if stop is None else ["settled"])
                self.assertEqual(ledger.entries[0].actual_source, "estimated")
                self.assertEqual(ledger.entries[0].actual["llm_requests"], 1)
                if stop is None:
                    self.assertEqual(ledger.entries[1].actual["llm_requests"], 1)
                    self.assertEqual(ledger.remaining("llm_requests"), 1)

    def test_provider_timeout_marks_attempt_unknown(self) -> None:
        for attempts in (1, 3):
            with self.subTest(attempts=attempts):
                ledger = BudgetLedger({"llm_requests": 3, "total_tokens": 100})
                client = LLMClient(LLMSettings(api_key="test-key", api_mode="chat",
                    retry_attempts=attempts), budget_ledger=ledger)
                with patch("simple_ar.integrations.llm._call_openai_sdk",
                           side_effect=RuntimeError("Request timed out.")) as openai_call, patch(
                           "simple_ar.integrations.llm.time.sleep"):
                    with self.assertRaises(LLMError) as caught:
                        client.ask("system", "user", label="timeout")
                if attempts == 3:
                    self.assertIsInstance(caught.exception.__cause__, BudgetUnknownError)
                    self.assertIn("preceding provider failure after 1 physical attempt(s): RuntimeError",
                                  str(caught.exception))
                openai_call.assert_called_once()
                self.assertEqual(len(ledger.entries), 1)
                self.assertEqual(ledger.entries[0].status, "unknown")
                self.assertEqual(ledger.remaining("llm_requests"), 2)
                self.assertIsNone(ledger.remaining("total_tokens"))

    def test_capped_timeout_can_retry_with_reserved_unknown_usage(self) -> None:
        ledger = BudgetLedger({"llm_requests": 3, "total_tokens": 100})
        client = LLMClient(
            LLMSettings(api_key="test-key", api_mode="chat", max_output_tokens=10,
                        retry_attempts=3, retry_base_delay_sec=0),
            budget_ledger=ledger,
        )
        response = {"choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}
        with patch("simple_ar.integrations.llm._call_openai_sdk",
                   side_effect=[TimeoutError("timed out"), response]) as call:
            self.assertEqual(client.ask("system", "user"), "ok")
        self.assertEqual(call.call_count, 2)
        self.assertEqual(ledger.remaining("llm_requests"), 1)
        self.assertEqual(ledger.entries[0].status, "unknown")
        self.assertEqual(ledger.unknown_dimensions(), ("total_tokens",))
        self.assertEqual(ledger.remaining("total_tokens"),
                         95 - ledger.entries[0].reserved["total_tokens"])

    def test_completed_corrections_scope_sdk_accounting_to_each_operation(self) -> None:
        from simple_ar.code_task.generation.agent_step import run_json_agent_step
        from simple_ar.code_task.generation.architecture import build_architecture_plan
        from simple_ar.code_task.generation.writer import _generate_implementation_contents, write_implementation_unit
        from simple_ar.code_task.editing.planning import _generate_plan_data

        # The shared step owns the uncertainty matrix; each consumer proves its
        # boundary placement, not a duplicate copy of every SDK fault scenario.
        cases_by_owner = {
            'step': ('correct', 'reserved', 'new_reserved', 'new_unknown', 'changed_history',
                'settled_to_unknown', 'transport_retry', 'budget', 'floor', 'unbounded_budget'),
            'architecture': ('correct', 'fallback', 'new_unknown'),
            'planning': ('correct', 'fallback', 'new_unknown'),
            'writer': ('correct', 'syntax', 'new_unknown', 'transport_retry', 'budget', 'floor'),
            'legacy_writer': ('correct', 'new_unknown'),
        }
        for owner, cases in cases_by_owner.items():
            for case in cases:
                with self.subTest(owner=owner, case=case), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    project = root / 'project'
                    project.mkdir()
                    original_bytes = b'VALUE = 1\n'
                    (project / 'main.py').write_bytes(original_bytes)
                    ledger = BudgetLedger({'llm_requests': 2 if case == 'budget' else 6,
                                           'total_tokens': 1000000})
                    ledger.reserve('old', {'llm_requests': 1, 'total_tokens': 100})
                    old = ledger.mark_unknown('old', reason='earlier failed transport',
                        known_actual={'llm_requests': 1}, retain_reservation=case != 'unbounded_budget')
                    old_record = old.to_dict()
                    if case == 'reserved':
                        ledger.reserve('pending', {'other': 1})
                    if case == 'settled_to_unknown':
                        ledger.reserve('settled', {'other': 1})
                        ledger.settle('settled', {'other': 1})
                    client = LLMClient(LLMSettings(api_key='test-key', api_mode='chat',
                        max_output_tokens=100, retry_attempts=2 if case == 'transport_retry' else 1,
                        retry_base_delay_sec=0), budget_ledger=ledger)
                    good = ({'ok': True} if owner in ('step', 'planning') else
                            {'files': [{'path': 'main.py', 'content': "print('ok')\n"}], 'unresolved': []})
                    if owner == 'writer':
                        good = {'files': [{'path': 'main.py', 'replacements': [
                            {'old_text': 'VALUE = 1', 'new_text': 'VALUE = 2'}]}], 'unresolved': []}
                    bad = ({'files': [{'path': 'main.py', 'replacements': [
                        {'old_text': 'missing original', 'new_text': 'VALUE = 2'}]}], 'unresolved': []}
                        if owner == 'writer' else {'files': []})
                    if case == 'syntax':
                        bad = {'files': [{'path': 'main.py', 'replacements': [
                            {'old_text': 'VALUE = 1', 'new_text': 'def broken(:'}]}], 'unresolved': []}
                    calls = []

                    def provider(*_args):
                        calls.append(True)
                        if case == 'transport_retry' and len(calls) == 1:
                            raise TimeoutError('response lost within this operation')
                        if len(calls) == 1:
                            if case in ('new_reserved', 'new_unknown'):
                                ledger.reserve('during-call', {'other': 1})
                                if case == 'new_unknown':
                                    ledger.mark_unknown('during-call', reason='new uncertainty',
                                                        known_actual={'other': 1})
                            elif case == 'changed_history':
                                old.reason = 'history changed during call'
                            elif case == 'settled_to_unknown':
                                ledger.entries[1].status = 'unknown'
                        rejected = len(calls) == 1 or case in ('fallback', 'transport_retry')
                        # Decode errors and complete parsed structural failures
                        # must both reach the same accounting boundary.
                        content = ('not a JSON response' if owner in ('step', 'planning')
                                   else json.dumps(bad)) if rejected else json.dumps(good)
                        return {'choices': [{'message': {'content': content}}],
                                'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}}

                    def invoke():
                        if owner == 'step':
                            return run_json_agent_step(client=client, system='s', prompt='p',
                                label='test', stage='test', attempt_index=1, retry_attempts=2,
                                max_output_tokens=100)
                        if owner == 'architecture':
                            return build_architecture_plan(contract={}, result_schema={}, resource_plan={},
                                domain_profile={}, client=client, retry_attempts=2, allow_fallback=True)
                        if owner == 'writer':
                            return write_implementation_unit(project_dir=project, goal='Change the value',
                                files=[{'path': 'main.py'}], source_windows=[], client=client,
                                artifact_dir=root / 'steps', retry_attempts=2,
                                original_files={'main.py': {'text': original_bytes.decode()}})
                        if owner == 'legacy_writer':
                            return _generate_implementation_contents(client=client, system='s', prompt='p',
                                planned=['main.py'], retry_attempts=2, legacy=True)
                        with patch.object(LLMClient, 'for_task', return_value=client):
                            return _generate_plan_data(Path(tmp), inputs={
                                'task_text': 'goal', 'index': {}, 'benchmark_command': '',
                                'run_context': {}, 'allowed_patterns': [], 'protected_patterns': []}, selected_files=[],
                                ask=lambda selected, **kw: selected.ask_json('s', 'p'),
                                offline=lambda **kw: {'offline': True}, plan_name='test', usage_stage='test',
                                model=None, use_llm=True, llm_client=client, llm_retry_attempts=2,
                                allow_llm_fallback=True, message_callback=None)

                    # The snapshot never bypasses the original request floor.
                    with ledger.protect_remaining({'llm_requests': 4 if case == 'floor' else 0}), patch(
                            'simple_ar.integrations.llm._call_openai_sdk', side_effect=provider) as sdk:
                        if case in ('correct', 'fallback'):
                            result = invoke()
                            if case == 'fallback':
                                self.assertEqual(result[1], 'fallback' if owner == 'architecture' else 'offline')
                            elif owner == 'writer':
                                self.assertEqual((project / 'main.py').read_bytes(), b'VALUE = 2\n')
                        else:
                            with self.assertRaises((LLMError, ValueError, SyntaxError)):
                                invoke()
                            self.assertEqual((project / 'main.py').read_bytes(), original_bytes)
                    expected_calls = (0 if case == 'unbounded_budget' else
                        2 if case in ('correct', 'fallback', 'transport_retry') else 1)
                    self.assertEqual(sdk.call_count, expected_calls)
                    if case != 'changed_history':
                        self.assertEqual(old.to_dict(), old_record)
                    self.assertEqual(old.status, 'unknown')
                    physical = [entry for entry in ledger.entries
                                if entry.reservation_id != 'old' and 'llm_requests' in entry.reserved]
                    self.assertEqual([entry.status for entry in physical],
                        ['unknown', 'settled'] if case == 'transport_retry' else
                        ['settled'] * sdk.call_count)

    def test_authentication_failure_releases_preflight_reservation(self) -> None:
        ledger = BudgetLedger({"llm_requests": 2, "total_tokens": 100})
        client = LLMClient(
            LLMSettings(api_key="test-key", api_mode="chat", retry_attempts=3),
            budget_ledger=ledger,
        )

        with patch(
            "simple_ar.integrations.llm._call_openai_sdk",
            side_effect=RuntimeError("Authentication failed: invalid API key."),
        ) as openai_call:
            with self.assertRaises(LLMError):
                client.ask("system", "user", label="auth")

        openai_call.assert_called_once()
        self.assertEqual(ledger.entries[0].status, "released")
        self.assertEqual(ledger.remaining("llm_requests"), 2)

    def test_ask_does_not_retry_permanent_auth_error(self) -> None:
        client = LLMClient(LLMSettings(api_key="test-key", api_mode="chat", retry_attempts=3))

        with patch(
            "simple_ar.integrations.llm._call_openai_sdk",
            side_effect=RuntimeError("Authentication failed: invalid API key."),
        ) as openai_call, patch("simple_ar.integrations.llm.time.sleep") as sleep:
            with self.assertRaises(LLMError):
                client.ask("system", "user", label="auth-test")

        self.assertEqual(openai_call.call_count, 1)
        sleep.assert_not_called()

    def test_gateway_model_routing_error_is_retryable(self) -> None:
        client = LLMClient(LLMSettings(api_key="test-key", api_mode="chat", retry_attempts=2))
        response = {"choices": [{"message": {"content": "ok"}}]}

        with patch(
            "simple_ar.integrations.llm._call_openai_sdk",
            side_effect=[
                RuntimeError("503 model not found: no available channel under distributor"),
                response,
            ],
        ) as openai_call, patch("simple_ar.integrations.llm.time.sleep") as sleep:
            self.assertEqual(client.ask("system", "user", label="gateway-retry"), "ok")

        self.assertEqual(openai_call.call_count, 2)
        sleep.assert_called_once()

    def test_overloaded_provider_retries_without_retrying_permanent_errors(self) -> None:
        client = LLMClient(LLMSettings(api_key="test-key", api_mode="chat", retry_attempts=2))
        response = {"choices": [{"message": {"content": "ok"}}]}

        with patch(
            "simple_ar.integrations.llm._call_openai_sdk",
            side_effect=[RuntimeError("Our servers are currently overloaded. Please try again later."), response],
        ) as call, patch("simple_ar.integrations.llm.time.sleep") as sleep:
            self.assertEqual(client.ask("system", "user", label="overload-retry"), "ok")

        self.assertEqual(call.call_count, 2)
        sleep.assert_called_once()

    def test_mid_response_disconnect_retries_and_preserves_unknown_usage(self) -> None:
        ledger = BudgetLedger({"llm_requests": 3, "total_tokens": 100})
        client = LLMClient(
            LLMSettings(api_key="test-key", api_mode="chat", retry_attempts=2,
                        max_output_tokens=10, retry_base_delay_sec=0.25),
            budget_ledger=ledger,
        )
        response = {"choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}
        disconnect = RuntimeError(
            "peer closed connection without sending complete message body (incomplete chunked read)"
        )
        with patch("simple_ar.integrations.llm._call_openai_sdk",
                   side_effect=[disconnect, response]) as call, patch(
                   "simple_ar.integrations.llm.time.sleep") as sleep:
            self.assertEqual(client.ask("system", "user", label="mid-response-retry"), "ok")
        self.assertEqual(call.call_count, 2)
        sleep.assert_called_once_with(0.25)
        self.assertEqual(ledger.entries[0].status, "unknown")
        self.assertEqual(ledger.entries[1].status, "settled")

    def test_openai_sdk_backend_disables_hidden_retries(self) -> None:
        with patch("openai.OpenAI") as openai_cls:
            openai_cls.return_value.chat.completions.create.return_value = {
                "choices": [{"message": {"content": "ok"}}]
            }

            _call_openai_sdk(
                "chat",
                {
                    "model": "gpt-5.1",
                    "api_key": "test-key",
                    "base_url": "https://example.test/v1",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )

        openai_cls.assert_called_once()
        client_kwargs = openai_cls.call_args.kwargs
        self.assertEqual(client_kwargs["max_retries"], 0)
        self.assertEqual(client_kwargs["base_url"], "https://example.test/v1")
        self.assertNotIn("timeout", client_kwargs)

    def test_stream_failure_retries_without_adopting_partial_text(self) -> None:
        from openai import APIError
        import httpx

        class InterruptedStream:
            closed = False

            def __iter__(self):
                yield {"choices": [{"delta": {"content": "unfinished"}}]}
                raise APIError("gateway decoder secret-fixture", request=httpx.Request(
                    "POST", "https://invalid.test"), body={"error": {
                        "message": ("other failure secret-fixture" if stop == "other"
                                    else "upstream overloaded secret-fixture")}})

            def close(self):
                self.closed = True

        for stop in (None, "requests", "tokens", "proxy", "other", "reconciliation"):
            with self.subTest(stop=stop):
                ledger = BudgetLedger({
                    "llm_requests": 1 if stop in {"requests", "proxy", "other"} else 3,
                    "total_tokens": (10 + estimate_tokens("system") + estimate_tokens("user")
                                     if stop == "tokens" else 200),
                })
                client = LLMClient(LLMSettings(api_key="test-key", api_mode="chat", stream=True,
                    max_output_tokens=10, retry_attempts=3,
                    proxy_env="API_PROXY" if stop == "proxy" else ""), budget_ledger=ledger)
                interrupted = InterruptedStream()
                complete = iter([
                    {"choices": [{"delta": {"content": "complete"}, "finish_reason": "stop"}]},
                    {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}},
                ])
                with patch("simple_ar.integrations.llm._call_openai_sdk",
                           side_effect=[interrupted, complete]) as call, patch(
                           "simple_ar.integrations.llm.time.sleep"), patch.dict(
                           os.environ, {"API_PROXY": "http://127.0.0.1:7890"}), patch.object(
                           ledger, "mark_unknown", wraps=ledger.mark_unknown,
                           side_effect=BudgetConflictError("fixture conflict")
                           if stop == "reconciliation" else None):
                    if stop is None:
                        self.assertEqual(client.ask("system", "user"), "complete")
                    else:
                        with self.assertRaises(LLMError) as caught:
                            client.ask("system", "user")
                        self.assertIsInstance(caught.exception.__cause__,
                            BudgetConflictError if stop == "reconciliation" else BudgetExceededError)
                        diagnostic = "".join(traceback.format_exception(caught.exception))
                        self.assertIn("LLMStreamError", diagnostic)
                        self.assertEqual("provider reported overload" in diagnostic, stop != "other")
                        self.assertIn("transport phase=possibly_dispatched", diagnostic)
                        self.assertNotIn("secret-fixture", diagnostic)
                        self.assertNotIn("unfinished", diagnostic)
                        self.assertNotIn("http://127.0.0.1:7890", diagnostic)
                        if stop != "reconciliation":
                            self.assertIn("provider attempt 2", str(caught.exception))
                            self.assertIn("after 1 physical attempt(s)", str(caught.exception))
                self.assertTrue(interrupted.closed)
                self.assertEqual(call.call_count, 2 if stop is None else 1)
                self.assertEqual([row.status for row in ledger.entries],
                    ["unknown", "settled"] if stop is None else
                    ["reserved"] if stop == "reconciliation" else ["unknown"])
                if stop != "reconciliation":
                    self.assertEqual(ledger.entries[0].actual, {"llm_requests": 1})
                    self.assertEqual(ledger.unknown_dimensions(), ("total_tokens",))
                    self.assertIn("LLMStreamError", ledger.entries[0].reason)
                if stop is None:
                    self.assertEqual(ledger.entries[1].actual["total_tokens"], 5)

    def test_connect_retry_ceiling_does_not_replay_dispatched_or_unknown_failures(self) -> None:
        import httpx

        response = {"choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}
        for failure, pre_dispatch in ((httpx.ConnectTimeout("TLS handshake"), True),
                                      (httpx.ReadTimeout("read interrupted"), False),
                                      (TimeoutError("unclassified timeout"), False)):
            with self.subTest(failure=type(failure).__name__):
                ledger = BudgetLedger({"llm_requests": 3, "total_tokens": 200})
                client = LLMClient(LLMSettings(api_key="test-key", api_mode="chat",
                    stream=True, max_output_tokens=10, retry_attempts=1,
                    connect_retry_attempts=3), budget_ledger=ledger)
                with patch("simple_ar.integrations.llm._call_openai_sdk",
                           side_effect=[failure, response]) as provider, \
                        patch("simple_ar.integrations.llm.time.sleep"):
                    if pre_dispatch:
                        self.assertEqual(client.ask("system", "user"), "ok")
                    else:
                        with self.assertRaises(LLMError):
                            client.ask("system", "user")
                self.assertEqual(provider.call_count, 2 if pre_dispatch else 1)
                self.assertEqual(ledger.entries[0].status, "settled" if pre_dispatch else "unknown")
                self.assertEqual(ledger.entries[0].actual,
                                 {"llm_requests": 1, "total_tokens": 0} if pre_dispatch
                                 else {"llm_requests": 1})
                if pre_dispatch:
                    self.assertEqual(ledger.entries[0].actual_source, "transport_pre_dispatch")
                    self.assertEqual(ledger.entries[1].actual["total_tokens"], 5)
                    self.assertEqual(ledger.unknown_dimensions(), ())
                else:
                    self.assertEqual(ledger.unknown_dimensions(), ("total_tokens",))

    def test_gateway_http2_failure_before_stream_iteration_is_uncertain_and_retryable(self) -> None:
        ledger = BudgetLedger({"llm_requests": 3, "total_tokens": 200})
        client = LLMClient(
            LLMSettings(api_key="test-key", api_mode="chat", stream=True,
                        max_output_tokens=10, retry_attempts=2), budget_ledger=ledger,
        )
        with patch("simple_ar.integrations.llm._call_openai_sdk", side_effect=[
            RuntimeError("Upstream HTTP/2 stream failed"),
            {"choices": [{"message": {"content": "ok"}}]},
        ]) as call, patch("simple_ar.integrations.llm.time.sleep"):
            self.assertEqual(client.ask("system", "user"), "ok")
        self.assertEqual(call.call_count, 2)
        self.assertEqual(ledger.entries[0].status, "unknown")

    def test_unclassified_provider_failure_does_not_imply_zero_charge_or_blind_retry(self) -> None:
        ledger = BudgetLedger({"llm_requests": 3, "total_tokens": 200})
        client = LLMClient(
            LLMSettings(api_key="test-key", api_mode="chat", max_output_tokens=10,
                        retry_attempts=3), budget_ledger=ledger,
        )
        with patch("simple_ar.integrations.llm._call_openai_sdk",
                   side_effect=RuntimeError("unexpected upstream failure")) as call:
            with self.assertRaises(LLMError):
                client.ask("system", "user")
        self.assertEqual(call.call_count, 1)
        self.assertEqual(ledger.entries[0].status, "unknown")
        self.assertEqual(ledger.entries[0].actual, {"llm_requests": 1})
        self.assertEqual(ledger.unknown_dimensions(), ("total_tokens",))

    def test_sdk_status_metadata_overrides_misleading_body_text(self) -> None:
        class ProviderFailure(RuntimeError):
            def __init__(self, status, message):
                super().__init__(message)
                self.status_code = status

        for status in (400, 401, 403):
            with self.subTest(status=status):
                ledger = BudgetLedger({"llm_requests": 3, "total_tokens": 200})
                client = LLMClient(LLMSettings(api_key="test-key", api_mode="chat",
                    retry_attempts=3), budget_ledger=ledger)
                with patch("simple_ar.integrations.llm._call_openai_sdk",
                           side_effect=ProviderFailure(status, "503 service unavailable mentioned in invalid input")) as call:
                    with self.assertRaises(LLMError):
                        client.ask("system", "user")
                self.assertEqual(call.call_count, 1)
                self.assertEqual(ledger.entries[0].status, "released")

        client = LLMClient(LLMSettings(api_key="test-key", api_mode="chat", retry_attempts=2))
        with patch("simple_ar.integrations.llm._call_openai_sdk", side_effect=[
            ProviderFailure(503, "unknown upstream response"),
            {"choices": [{"message": {"content": "ok"}}]},
        ]) as call, patch("simple_ar.integrations.llm.time.sleep"):
            self.assertEqual(client.ask("system", "user"), "ok")
        self.assertEqual(call.call_count, 2)

    def test_openai_sdk_backend_passes_explicit_timeout_only(self) -> None:
        import httpx

        async def chunks():
            yield {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}

        class Stream:
            def __init__(self):
                self.close = AsyncMock()

            def __aiter__(self):
                return chunks()

        for value, connect in ((123, 20.0), (0.5, 0.5)):
            for mode, streamed in (("chat", False), ("responses", False), ("chat", True)):
                with self.subTest(timeout=value, mode=mode, streamed=streamed), \
                        patch("openai.OpenAI") as sync_factory, \
                        patch("openai.AsyncOpenAI") as async_factory, \
                        patch("openai.DefaultAsyncHttpxClient"), patch("openai.DefaultHttpxClient"):
                    factory = async_factory if streamed else sync_factory
                    sdk = factory.return_value
                    create = sdk.responses.create if mode == "responses" else sdk.chat.completions.create
                    if streamed:
                        stream = Stream()
                        sdk.chat.completions.create = create = AsyncMock(return_value=stream)
                        sdk.close = AsyncMock()
                    else:
                        create.return_value = {"output_text": "ok"} if mode == "responses" else {
                            "choices": [{"message": {"content": "ok"}}]}
                    request = {"model": "fixture", "api_key": "test-key", "timeout": value,
                               "input" if mode == "responses" else "messages": [
                                   {"role": "user", "content": "hello"}]}
                    if streamed:
                        request["stream"] = True

                    result = _call_openai_sdk(mode, request)

                    self.assertIn("ok", str(result))
                    factory.assert_called_once()
                    (sync_factory if streamed else async_factory).assert_not_called()
                    timeout = factory.call_args.kwargs["timeout"]
                    self.assertIsInstance(timeout, httpx.Timeout)
                    self.assertEqual(timeout.connect, connect)
                    self.assertEqual((timeout.read, timeout.write, timeout.pool), (value,) * 3)
                    self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
                    create.assert_called_once()
                    self.assertNotIn("timeout", create.call_args.kwargs)
                    self.assertEqual(request["timeout"], value)
                    if streamed:
                        create.assert_awaited_once()
                        stream.close.assert_awaited_once()
                        sdk.close.assert_awaited_once()
                    else:
                        sdk.close.assert_called_once()

    def test_installed_sdk_connect_cap_and_slow_stream_on_loopback(self) -> None:
        from contextlib import contextmanager
        import socket
        import threading
        import httpx
        from openai import APITimeoutError, AsyncOpenAI, DefaultAsyncHttpxClient
        from simple_ar.integrations.llm import _transport_failure_phase

        @contextmanager
        def loopback(stall_tls):
            stop = threading.Event()
            accepted, failures = [], []
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(0.1)

            def serve():
                try:
                    while not stop.is_set():
                        try:
                            connection, _ = listener.accept()
                        except socket.timeout:
                            continue
                        with connection:
                            accepted.append(1)
                            connection.settimeout(1)
                            if stall_tls:
                                # Accept TCP but never answer the TLS ClientHello.
                                stop.wait(3)
                                continue
                            with connection.makefile("rb") as incoming:
                                headers = []
                                while line := incoming.readline():
                                    if line == b"\r\n":
                                        break
                                    headers.append(line)
                                length = next(int(line.split(b":", 1)[1]) for line in headers
                                              if line.lower().startswith(b"content-length:"))
                                incoming.read(length)
                            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                                               b"Connection: close\r\n\r\n")
                            # A read gap exceeds the connect cap, within the read allowance.
                            if stop.wait(0.15):
                                return
                            chunk = {"id": "fixture", "object": "chat.completion.chunk", "created": 0,
                                     "model": "fixture", "choices": [{"index": 0,
                                     "delta": {"content": "ok"}, "finish_reason": "stop"}]}
                            connection.sendall(b"data: " + json.dumps(chunk).encode()
                                               + b"\n\ndata: [DONE]\n\n")
                except Exception as exc:
                    failures.append(exc)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            try:
                scheme = "https" if stall_tls else "http"
                yield f"{scheme}://127.0.0.1:{listener.getsockname()[1]}/v1", accepted
            finally:
                stop.set()
                thread.join(2)
                listener.close()
                self.assertFalse(thread.is_alive(), "loopback server did not stop")
                self.assertEqual(failures, [])

        for stall_tls in (True, False):
            with self.subTest(stall_tls=stall_tls), loopback(stall_tls) as (url, accepted), \
                    patch("simple_ar.integrations.llm._SDK_CONNECT_TIMEOUT_SEC", 0.05), \
                    patch("openai.AsyncOpenAI", wraps=AsyncOpenAI) as factory, \
                    patch("openai.DefaultAsyncHttpxClient", side_effect=lambda **kwargs:
                          DefaultAsyncHttpxClient(trust_env=False, **kwargs)):
                request = {"api_key": "local-fixture", "base_url": url, "model": "fixture",
                           "messages": [{"role": "user", "content": "hello"}],
                           "stream": True, "timeout": 2.0}
                started = time.monotonic()
                if stall_tls:
                    with self.assertRaises(APITimeoutError) as caught:
                        _call_openai_sdk("chat", request)
                    self.assertIsInstance(caught.exception.__cause__, httpx.ConnectTimeout)
                    self.assertEqual(_transport_failure_phase(caught.exception), "not_dispatched")
                    self.assertLess(time.monotonic() - started, request["timeout"])
                else:
                    self.assertIn("ok", str(_call_openai_sdk("chat", request)))
                    self.assertGreater(time.monotonic() - started, 0.05)
                factory.assert_called_once()
                self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
                self.assertEqual(accepted, [1])

    def test_estimate_tokens_is_deterministic_and_nonzero_for_text(self) -> None:
        self.assertEqual(estimate_tokens(""), 0)
        self.assertGreaterEqual(estimate_tokens("hello"), 1)


if __name__ == "__main__":
    unittest.main()
