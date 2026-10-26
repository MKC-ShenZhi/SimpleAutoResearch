"""Connection selection and compatibility; no paid requests or model fixtures."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from simple_ar.integrations.llm import LLMClient, LLMError
from simple_ar.integrations.model_profiles import load_model_catalog, ModelConfigError, model_connections_compatible


CATALOG = '''version = 1
[routes]
default = "writer"
code = "coder"
image = "artist"
[profiles.writer]
api = "openai_chat"
base_url = "https://writer.example/v1"
model = "writer-model"
api_key_env = "WRITER_KEY"
capabilities = ["text"]
stream = true
retry_attempts = 1
[profiles.coder]
api = "openai_chat"
base_url = "https://coder.example/v1"
model = "coder-model"
api_key_env = "CODER_KEY"
capabilities = ["text", "code"]
retry_attempts = 1
[profiles.artist]
api = "openai_images"
base_url = "https://images.example/v1"
model = "image-model"
api_key_env = "IMAGE_KEY"
capabilities = ["image_generate", "image_edit"]
'''


def png_bytes(size):
    from PIL import Image
    payload = io.BytesIO()
    Image.new("RGB", size, "white").save(payload, format="PNG")
    return payload.getvalue()


class ModelProfileTests(unittest.TestCase):
    def test_run_image_async_pending_reservation_resume_and_completed_restore(self):
        from PIL import Image
        from simple_ar.core.budget import BudgetLedger
        from simple_ar.core.capabilities import ArtifactStore
        from simple_ar.integrations.image_tasks import ImageTaskError
        from simple_ar.integrations.images import run_image
        sync_catalog = load_model_catalog(self.path)
        os.environ["IMAGE_KEY"] = "image-secret"
        for api in ("openai_images", "cctq_images_async"):
            with self.subTest(preflight_api=api):
                artist = sync_catalog.profiles["artist"].model_copy(update={"api": api, "http2": True})
                preflight_catalog = sync_catalog.model_copy(update={"profiles": {**sync_catalog.profiles, "artist": artist}})
                with patch("simple_ar.integrations.images.import_module", side_effect=ImportError):
                    store = self._assert_image_preflight_rejected(preflight_catalog, api + "-preflight",
                        capacity=2, message="no image submission attempted")
                self.assertFalse(store.root.exists())
        self._write_catalog('api = "openai_images"', 'api = "cctq_images_async"\nproxy_env = "IMAGE_PROXY"')
        os.environ["IMAGE_PROXY"] = "http://127.0.0.1:7890"
        catalog = load_model_catalog(self.path)
        payload = png_bytes((8, 8))
        store = ArtifactStore(self.root / "async-integration")
        ledger_path = self.root / "async-integration.budget.json"
        kwargs = dict(store=store, reservation_id="image", reserves={"image_requests": 1},
                      ledger=BudgetLedger({"image_requests": 1}, storage_path=ledger_path))

        def helper(**request):
            self.assertEqual(request["proxy"], os.environ["IMAGE_PROXY"])
            self.assertNotIn("proxy", json.dumps(store.read_json("request.json")))
            self.assertNotIn(os.environ["IMAGE_PROXY"], store.read_text("request.json"))
            self.assertEqual(request["store"].read_json("request.json")["api"], "cctq_images_async")
            if not store.exists("image_task.json"):
                store.write_json("image_task.json", {"id": "pending_1", "status": "in_progress"})
                raise ImageTaskError("Polling interrupted; saved task retained")
            self.assertEqual(store.read_json("image_task.json")["id"], "pending_1")
            store.write_json("image_task.json", {"id": "pending_1", "status": "completed"})
            return payload, {"total_tokens": 5}, "pending_1"

        with patch("simple_ar.integrations.image_tasks.request_task_image", side_effect=helper) as send, \
                patch("simple_ar.integrations.images.import_module") as http2_dependency, \
                patch("simple_ar.integrations.images.OpenAI") as sdk:
            failed = run_image(catalog, "Draw an overview", **kwargs)
            self.assertEqual(failed.status, "failed", failed.diagnostics)
            self.assertFalse(any(ref.kind == "image" for ref in failed.artifacts))
            kwargs["ledger"] = BudgetLedger.load(ledger_path)
            self.assertEqual(kwargs["ledger"].entries[0].status, "reserved")
            saved_request = store.read_json("request.json")
            artist = catalog.profiles["artist"].model_copy(update={"http2": True, "request_timeout_sec": 900.0})
            catalog = catalog.model_copy(update={"profiles": {**catalog.profiles, "artist": artist}})
            completed = run_image(catalog, "Draw an overview", **kwargs)
            self.assertEqual(completed.status, "completed", completed.diagnostics)
            http2_dependency.assert_called_once_with("h2.connection")
            self.assertEqual(store.read_json("request.json"), saved_request)
            image_ref = next(ref for ref in completed.artifacts if ref.kind == "image")
            with Image.open(store.require(image_ref)) as image:
                self.assertEqual(image.size, (8, 8))
                image.verify()
            kwargs["ledger"] = BudgetLedger.load(ledger_path)
            self.assertEqual(len(kwargs["ledger"].entries), 1)
            entry = kwargs["ledger"].entries[0]
            self.assertEqual((entry.status, entry.actual["image_requests"], entry.provider_call_id),
                             ("settled", 1, "pending_1"))
            self.assertEqual(run_image(catalog, "Draw an overview", **kwargs), completed)
            with patch("simple_ar.integrations.images.import_module", side_effect=ImportError):
                self.assertEqual(run_image(catalog, "Draw an overview", **kwargs), completed)
            self.assertEqual(send.call_count, 2)
            sdk.assert_not_called()

        # The native consumer reuses this adapter/ledger, rather than owning a
        # second submission state. Editing creates another version, not a patch.
        from simple_ar.tools.workspace import WorkspaceToolGateway
        from simple_ar.tools.images import ImageToolGateway
        root = self.root / "native-images"
        root.mkdir()
        native_ledger = BudgetLedger({"image_requests": 2}, storage_path=root / "budget.json")
        workspace = WorkspaceToolGateway(root, {}, native_ledger)
        gateway = ImageToolGateway(workspace, native_ledger, fallback_models_config=str(self.path.resolve()))

        def native_helper(**request):
            image_store = request["store"]
            if not image_store.exists("image_task.json"):
                image_store.write_json("image_task.json", {"id": image_store.root.parent.name,
                    "status": "in_progress"})
                raise ImageTaskError("Polling interrupted")
            if request["source"]:
                self.assertEqual(request["source"].read_bytes(), payload)
            image_store.write_json("image_task.json", {"id": image_store.root.parent.name,
                "status": "completed"})
            return payload, {}, image_store.root.parent.name

        with patch("simple_ar.tools.images.load_model_catalog", return_value=catalog) as resolve_catalog, \
                patch("simple_ar.integrations.images.import_module"), \
                patch("simple_ar.integrations.image_tasks.request_task_image", side_effect=native_helper) as send:
            first = gateway.call("create_image", {"prompt": "Draw an overview"}, identity="first")
            resolve_catalog.assert_called_with(fallback_path=str(self.path.resolve()))
            self.assertEqual(first.status, "error")
            self.assertEqual(gateway.call("create_image", {"prompt": "Try again"}, identity="duplicate").status, "error")
            self.assertEqual(send.call_count, 1)
            self.assertEqual(len(native_ledger.entries), 1)
            restored = gateway.call("resume_image", {"version": "first"}, identity="poll")
            self.assertEqual(restored.status, "ok", restored.error)
            self.assertEqual(gateway.call("resume_image", {"version": "first"}, identity="again").data,
                restored.data)
            self.assertEqual(send.call_count, 2)
            image_path = next(p for p in restored.artifacts if p.endswith(".png"))
            original = workspace.resolve(image_path).read_bytes()
            edited = gateway.call("create_image", {"prompt": "Change layout", "input_image": image_path},
                identity="edit")
            self.assertEqual(edited.status, "error")
            self.assertEqual(gateway.call("resume_image", {"version": "edit"}, identity="edit-poll").status, "ok")
            self.assertEqual(workspace.resolve(image_path).read_bytes(), original)
            self.assertEqual(len(native_ledger.entries), 2)
            self.assertEqual(gateway.call("resume_image", {"version": "not-owned"}, identity="bad").status, "error")
            self.assertEqual(gateway.call("create_image", {"prompt": "Read secret", "input_image": "workspace/.env"},
                identity="secret").status, "error")
            self.assertEqual(send.call_count, 4)

    def test_async_images_poll_and_download_recovery_never_resubmit(self):
        import httpx
        from simple_ar.core.capabilities import ArtifactStore
        from simple_ar.integrations import image_tasks
        store = ArtifactStore(self.root / "async-generation")
        clock, calls, stage = [0.0], [], ["timeout"]
        real_client = httpx.AsyncClient
        png = b"caller-validates-image-bytes"
        os.environ["IMAGE_PROXY"] = "http://127.0.0.1:7890"
        profile = load_model_catalog().profiles["artist"].model_copy(update={"proxy_env": "IMAGE_PROXY"})

        def send(request):
            calls.append((request.method, request.url.path))
            self.assertEqual(request.headers["Authorization"], "Bearer image-secret")
            self.assertEqual(request.url.host, "images.example")
            saved = store.read_json("image_task.json")
            if request.method == "POST":
                self.assertEqual(saved["phase"], "started")
                self.assertNotIn("id", saved)
                self.assertEqual(json.loads(request.content), {
                    "model": "artist", "prompt": "overview", "async": True, "quality": "low"})
                return httpx.Response(202, json={"object": "image.task", "id": "task_1-A",
                    "status": "queued", "poll_url": "https://elsewhere.example/secret"})
            self.assertEqual(saved["id"], "task_1-A")
            self.assertLessEqual(request.extensions["timeout"]["read"], 12)
            if request.url.path.endswith("/files/0"):
                if stage[0] == "redirect":
                    return httpx.Response(302, headers={"Location": "https://elsewhere.example/secret"})
                return httpx.Response(200, content=png)
            return httpx.Response(200, json={"object": "image.task", "id": "task_1-A",
                "status": "in_progress" if stage[0] == "timeout" else "completed",
                "result": {"data": [{"url": "https://elsewhere.example/secret"}], "usage": {
                    "total_tokens": 7, "input_tokens_details": {"image_tokens": 2},
                    "text": "image-secret", "image-secret": 99, "flag": True}},
                "error": {"message": "image-secret provider body"}})

        def factory(**kwargs):
            self.assertFalse(kwargs["follow_redirects"])
            self.assertNotIn("trust_env", kwargs)  # Standard SDK-compatible environment routing.
            self.assertEqual(kwargs.pop("proxy"), profile.resolve_proxy())
            return real_client(transport=httpx.MockTransport(send), **kwargs)

        kwargs = dict(base_url="https://images.example/v1", key="image-secret", model="artist",
                      prompt="overview", source=None, options={"quality": "low"},
                      timeout=12, http2=False, store=store, proxy=profile.resolve_proxy())
        with patch.object(image_tasks.httpx, "AsyncClient", side_effect=factory), \
                patch.object(image_tasks.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(image_tasks.asyncio, "sleep", new=AsyncMock(side_effect=lambda n: clock.__setitem__(0, clock[0] + n))):
            with self.assertRaisesRegex(image_tasks.ImageTaskError, "deadline"):
                image_tasks.request_task_image(**kwargs)
            self.assertEqual(clock[0], 12)
            stage[0] = "redirect"
            with self.assertRaisesRegex(image_tasks.ImageTaskError, "download failed"):
                image_tasks.request_task_image(**kwargs)
            self.assertEqual(store.read_json("image_task.json")["status"], "completed")
            stage[0] = "complete"
            recovery_start = len(calls)
            self.assertEqual(image_tasks.request_task_image(**kwargs),
                (png, {"total_tokens": 7, "input_tokens_details": {"image_tokens": 2}}, "task_1-A"))
            self.assertEqual(calls[recovery_start:], [("GET", "/v1/images/tasks/task_1-A/files/0")])
        self.assertEqual(sum(method == "POST" for method, _ in calls), 1)
        self.assertTrue(all(path.startswith("/v1/images/") for _, path in calls))
        saved = store.read_text("image_task.json")
        self.assertNotIn("image-secret", saved)
        self.assertNotIn("elsewhere", saved)
        self.assertNotIn(os.environ["IMAGE_PROXY"], saved)

        # One wall deadline must cancel blocked network work, not merely check
        # between returned chunks. Recovery keeps the same accepted task.
        import asyncio
        import time
        for blocked_at in ('submission', 'polling', 'download'):
            with self.subTest(blocked_at=blocked_at):
                blocked_store = ArtifactStore(self.root / ('blocked-' + blocked_at))
                requests, clients = [], []
                phase = [blocked_at]

                class EmptyKeepalive(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        while True:
                            await asyncio.sleep(0.002)
                            yield b''

                async def respond(request):
                    requests.append(request.method)
                    if phase[0] == 'submission' or (phase[0] == 'polling' and request.method == 'GET'):
                        await asyncio.Event().wait()
                    if request.method == 'POST':
                        return httpx.Response(202, json={'object': 'image.task', 'id': 'fixed', 'status': 'queued'})
                    if request.url.path.endswith('/files/0'):
                        return httpx.Response(200, stream=EmptyKeepalive()) if phase[0] == 'download' else httpx.Response(200, content=png)
                    return httpx.Response(200, json={'object': 'image.task', 'id': 'fixed', 'status': 'completed', 'result': {'usage': {}}})

                def blocking_client(**values):
                    values.pop('proxy', None)  # Mock transport, no real proxy mount.
                    client = real_client(transport=httpx.MockTransport(respond), **values)
                    clients.append(client)
                    return client

                with patch.object(image_tasks.httpx, 'AsyncClient', side_effect=blocking_client):
                    started = time.monotonic()
                    blocked_kwargs = {**kwargs, 'store': blocked_store, 'timeout': 0.05}
                    with self.assertRaisesRegex(image_tasks.ImageTaskError, 'deadline'):
                        image_tasks.request_task_image(**blocked_kwargs)
                    self.assertLess(time.monotonic() - started, 1)
                    self.assertTrue(clients[0].is_closed)
                    phase[0] = 'complete'
                    if blocked_at == 'submission':
                        with self.assertRaises(image_tasks.ImageTaskInterrupted):
                            image_tasks.request_task_image(**blocked_kwargs)
                    else:
                        self.assertEqual(blocked_store.read_json('image_task.json')['id'], 'fixed')
                        self.assertEqual(image_tasks.request_task_image(**blocked_kwargs), (png, {}, 'fixed'))
                    self.assertEqual(requests.count('POST'), 1)

    def test_async_images_interrupted_submission_and_multipart_edit(self):
        import httpx
        from simple_ar.core.capabilities import ArtifactStore
        from simple_ar.integrations import image_tasks
        real_client, calls = httpx.AsyncClient, []
        store = ArtifactStore(self.root / "async-interrupted")
        kwargs = dict(base_url="https://images.example/v1", key="image-secret", model="artist",
                      prompt="overview", source=None, options={}, timeout=10, http2=False, store=store)

        def interrupted(request):
            calls.append(request.method)
            self.assertEqual(store.read_json("image_task.json")["phase"], "started")
            raise httpx.ReadTimeout("image-secret provider body", request=request)

        with patch.object(image_tasks.httpx, "AsyncClient", side_effect=lambda **kw:
                          real_client(transport=httpx.MockTransport(interrupted), **kw)):
            with self.assertRaises(image_tasks.ImageTaskError) as caught:
                image_tasks.request_task_image(**kwargs)
            self.assertNotIn("image-secret", str(caught.exception))
            with self.assertRaises(image_tasks.ImageTaskInterrupted):
                image_tasks.request_task_image(**kwargs)
        self.assertEqual(calls, ["POST"])
        source = self.root / "input.png"
        source.write_bytes(png_bytes((2, 2)))
        edit_store = ArtifactStore(self.root / "async-edit")
        kwargs.update(source=source, store=edit_store)

        def edit(request):
            if request.method == "POST":
                self.assertEqual(request.url.path, "/v1/images/edits")
                self.assertEqual(edit_store.read_json("image_task.json")["phase"], "started")
                self.assertIn('multipart/form-data', request.headers["content-type"])
                self.assertIn(b'name="async"\r\n\r\ntrue', request.content)
                self.assertIn(b'name="image"; filename="input.png"', request.content)
                self.assertIn(source.read_bytes(), request.content)
                return httpx.Response(202, json={"object": "image.task", "id": "edit_1", "status": "queued"})
            self.assertEqual(edit_store.read_json("image_task.json")["id"], "edit_1")
            if request.url.path.endswith("/files/0"):
                return httpx.Response(200, content=source.read_bytes())
            return httpx.Response(200, json={"object": "image.task", "id": "edit_1",
                                            "status": "completed", "result": {"usage": {}}})

        with patch.object(image_tasks.httpx, "AsyncClient", side_effect=lambda **kw:
                          real_client(transport=httpx.MockTransport(edit), **kw)):
            self.assertEqual(image_tasks.request_task_image(**kwargs), (source.read_bytes(), {}, "edit_1"))
            kwargs["prompt"] = "different request"
            with self.assertRaisesRegex(image_tasks.ImageTaskError, "differs"):
                image_tasks.request_task_image(**kwargs)
            old = ArtifactStore(self.root / "old-sync")
            old.write_json("request.json", {"old": "synchronous"})
            kwargs["store"] = old
            with self.assertRaisesRegex(image_tasks.ImageTaskError, "synchronous"):
                image_tasks.request_task_image(**kwargs)

    def test_images_generate_edit_and_saved_recovery_use_one_request_each(self):
        import base64
        from types import SimpleNamespace
        from simple_ar.core.budget import BudgetLedger
        from simple_ar.core.capabilities import ArtifactStore
        from simple_ar.integrations.images import run_image
        os.environ["IMAGE_KEY"] = "image-secret"
        os.environ["IMAGE_PROXY"] = "http://127.0.0.1:7890"
        self._write_catalog('api = "openai_images"', 'api = "openai_images"\nproxy_env = "IMAGE_PROXY"')
        response = SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(png_bytes((8, 8))).decode())],
            usage=None, _request_id="request-test")
        catalog = load_model_catalog(self.path)
        with patch("simple_ar.integrations.images.OpenAI") as factory, \
                patch("simple_ar.integrations.images.DefaultHttpxClient") as transport:
            client = factory.return_value.__enter__.return_value
            client.images.generate.return_value = response
            client.images.edit.return_value = response
            original = None
            for version in ("original", "edited"):
                store = ArtifactStore(self.root / version)
                ledger = BudgetLedger({"image_requests": 1}, storage_path=self.root / (version + ".json"))
                kwargs = dict(store=store, ledger=ledger, reservation_id=version,
                    reserves={"image_requests": 1})
                if original:
                    kwargs.update(input_ref=original[1], input_store=original[0])
                result = run_image(catalog, "Draw a method overview", **kwargs)
                self.assertEqual(result.status, "completed", result.diagnostics)
                self.assertEqual(run_image(catalog, "Draw a method overview", **kwargs), result)
                self.assertEqual(ledger.entries[0].actual["image_requests"], 1)
                self.assertIsNone(result.usage["cost"])
                self.assertEqual(transport.call_args.kwargs["proxy"], os.environ["IMAGE_PROXY"])
                self.assertNotIn("proxy", json.dumps(store.read_json("request.json")))
                self.assertNotIn(os.environ["IMAGE_PROXY"], store.read_text("request.json"))
                original = (store, result.artifacts[0])
            self.assertEqual(client.images.generate.call_count, 1)
            self.assertEqual(client.images.edit.call_count, 1)
            self.assertEqual(factory.call_args.kwargs["max_retries"], 0)

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.path = self.root / "models.toml"
        self.path.write_text(CATALOG, encoding="utf-8")
        self.stack.enter_context(patch.dict(os.environ, {
            "SIMPLE_AR_MODELS_CONFIG": str(self.path), "WRITER_KEY": "writer-secret",
            "CODER_KEY": "coder-secret", "OPENAI_API_KEY": "legacy-secret",
            "SIMPLE_AR_MODEL": "legacy-model"}, clear=True))
        self.stack.enter_context(patch("simple_ar.integrations.llm.load_dotenv"))
        self.stack.enter_context(patch("simple_ar.integrations.model_profiles.Path.home", return_value=self.root))

    def _write_catalog(self, original, replacement, count=-1):
        self.path.write_text(CATALOG.replace(original, replacement, count), encoding="utf-8")

    def _assert_image_preflight_rejected(self, catalog, name, *, capacity=1, message=""):
        from simple_ar.core.budget import BudgetLedger
        from simple_ar.core.capabilities import ArtifactStore
        from simple_ar.integrations.images import ImagesError, run_image
        store = ArtifactStore(self.root / name)
        ledger = BudgetLedger({"image_requests": capacity}, storage_path=self.root / (name + "-budget.json"))
        with patch("simple_ar.integrations.images.OpenAI") as sync_send, \
                patch("simple_ar.integrations.image_tasks.request_task_image") as async_send:
            with self.assertRaisesRegex(ImagesError, message):
                run_image(catalog, "Draw an overview", store=store, ledger=ledger,
                    reservation_id="preflight", reserves={"image_requests": 1})
            sync_send.assert_not_called()
            async_send.assert_not_called()
        self.assertEqual(ledger.entries, ())
        self.assertFalse(store.exists("request.json"))
        return store

    def test_routed_requests_use_whole_connection_and_preserve_usage(self):
        self.path.write_text(CATALOG.replace('api_key_env = "WRITER_KEY"',
            'api_key_env = "WRITER_KEY"\nconnect_timeout_sec = 45.0').replace(
            'api_key_env = "CODER_KEY"', 'api_key_env = "CODER_KEY"\nconnect_timeout_sec = 60.0'), encoding="utf-8")
        parent_usage, task_usage = [], []
        parent = LLMClient.from_env(usage_callback=parent_usage.append)
        task = LLMClient.for_task(client=parent, model=parent.model, usage_callback=task_usage.append)
        response = {"choices": [{"message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}}
        with patch("simple_ar.integrations.llm._call_openai_sdk", return_value=response) as send:
            self.assertEqual(parent.ask("system", "write"), "ok")
            self.assertEqual(task.ask("system", "edit"), "ok")
        requests = [call.args[1] for call in send.call_args_list]
        self.assertEqual([(r["model"], r["base_url"], r["api_key"]) for r in requests], [
            ("writer-model", "https://writer.example/v1", "writer-secret"),
            ("coder-model", "https://coder.example/v1", "coder-secret")])
        self.assertTrue(requests[0]["stream"])
        self.assertEqual([call.kwargs["connect_timeout_sec"] for call in send.call_args_list], [45.0, 60.0])
        self.assertTrue(all("connect_timeout_sec" not in request for request in requests))
        self.assertEqual(len(parent_usage), 2)
        self.assertEqual(len(task_usage), 1)
        self.assertEqual(task_usage[0].model, "coder-model")
        ledger = object()
        bounded = parent.with_budget(ledger, session_id="session")
        nested = LLMClient.for_task(client=bounded)
        self.assertIs(nested._budget_ledger, ledger)
        self.assertEqual(nested._budget_session_id, "session")
        # Writing is a text consumer with an optional independent connection,
        # not another capability enum or a second resource owner.
        for configured in (False, True):
            with self.subTest(writing_route=configured):
                self.path.write_text(CATALOG.replace('code = "coder"',
                    'code = "coder"\nwriting = "coder"') if configured else CATALOG, encoding="utf-8")
                parent = LLMClient.from_env(model="profile:writer").with_budget(ledger, session_id="writing-task", attempt_id="compose")
                writing = LLMClient.for_task(client=parent, purpose="writing")
                self.assertEqual(writing.model, "coder-model" if configured else "writer-model")
                self.assertEqual(writing._settings.api_key, "coder-secret" if configured else "writer-secret")
                self.assertIs(writing._budget_ledger, ledger)
                self.assertEqual((writing._budget_session_id, writing._budget_attempt_id), ("writing-task", "compose"))
                self.assertEqual(LLMClient.for_task(client=writing, purpose="writing").model, writing.model)
                self.assertEqual("writing_route" in parent.connection_binding(), configured)
        # Without a route, preserve an explicit nondefault parent too.
        self.path.write_text(CATALOG, encoding="utf-8")
        selected = LLMClient.from_env("profile:coder")
        self.assertEqual(LLMClient.for_task(client=selected, purpose="writing").model, "coder-model")

    def test_named_selection_is_explicit_and_never_falls_back(self):
        for selector in ("profile:coder", "route:code"):
            with self.subTest(selector=selector):
                self.assertEqual(LLMClient.from_env(model=selector).model, "coder-model")
        for selector in ["profile:missing", "different-raw-model", "profile:artist"]:
            with self.subTest(selector=selector), self.assertRaises(LLMError):
                LLMClient.from_env(model=selector)
        for api in ("openai_chat", "openai_responses"):
            with self.subTest(api=api):
                self.path.write_text(CATALOG.replace('api = "openai_chat"', f'api = "{api}"', 1)
                    .replace('stream = true', 'stream = false\nconnect_timeout_sec = 45'), encoding="utf-8")
                configured = LLMClient.from_env()
                self.assertEqual(configured._settings.connect_timeout_sec, 45.0)
                self.assertEqual(configured._settings.api_mode, "chat" if api == "openai_chat" else "responses")
                self.assertEqual(configured.connection_binding()["settings"]["connect_timeout_sec"], 45.0)
        self.path.write_text(CATALOG, encoding="utf-8")
        del os.environ["CODER_KEY"]
        with self.assertRaisesRegex(LLMError, "CODER_KEY"):
            LLMClient.from_env(purpose="code")
        os.environ["SIMPLE_AR_MODELS_CONFIG"] = str(self.root / "missing")
        with self.assertRaises(LLMError):
            LLMClient.from_env()

    def test_binding_excludes_secrets_allows_key_rotation_and_detects_changes(self):
        client = LLMClient.from_env()
        binding = client.connection_binding()
        self.assertNotIn("http2", binding["settings"])
        self.assertNotIn("http2", binding["code_route"]["connection"])
        self.assertNotIn("connect_timeout_sec", binding["settings"])
        self.assertNotIn("connect_timeout_sec", binding["code_route"]["connection"])
        self.assertNotIn("connect_timeout_sec", load_model_catalog().profiles["writer"].model_dump())
        self.assertNotIn("connect_timeout_sec", load_model_catalog().profiles["writer"].text_settings())
        self.assertNotIn("writer-secret", json.dumps(binding))
        self.assertNotIn("writer-secret", repr(client._settings))
        self.path.write_text(CATALOG.replace('api_key_env = "WRITER_KEY"',
                            'api_key_env = "WRITER_KEY"\nproxy_env = "WRITER_PROXY"').replace(
                            'api_key_env = "CODER_KEY"', 'api_key_env = "CODER_KEY"\nproxy_env = "CODER_PROXY"'),
                            encoding="utf-8")
        self.assertEqual(LLMClient.from_env().connection_binding(), binding)
        self.assertNotIn("proxy_env", binding["settings"])
        self.assertNotIn("proxy_env", binding["code_route"]["connection"])
        os.environ["WRITER_KEY"] = "rotated"
        self.assertEqual(LLMClient.from_env().connection_binding(), binding)
        self._write_catalog("image-model", "new-image-model")
        self.assertEqual(LLMClient.from_env().connection_binding(), binding)
        self._write_catalog('retry_attempts = 1',
            'retry_attempts = 3\nconnect_retry_attempts = 4\nrequest_timeout_sec = 1200.0\nconnect_timeout_sec = 45.0\nmax_output_tokens = 16384\ncontext_input_tokens = 64000')
        tuned = LLMClient.from_env().connection_binding()
        self.assertNotEqual(tuned, binding)
        self.assertTrue(model_connections_compatible(binding, tuned))
        self.assertTrue(model_connections_compatible(tuned, binding))
        # Streaming is transport, not a model/protocol/route change. The
        # original snapshot remains intact when transport is retuned.
        self.path.write_text(self.path.read_text(encoding='utf-8').replace(
            'stream = true', 'stream = false'), encoding='utf-8')
        nonstream = LLMClient.from_env()
        self.assertFalse(nonstream._settings.stream)
        self.assertTrue(model_connections_compatible(binding, nonstream.connection_binding()))
        self.assertTrue(model_connections_compatible(nonstream.connection_binding(), binding))
        self.assertEqual(tuned["settings"]["connect_timeout_sec"], 45.0)
        self.assertEqual(tuned["code_route"]["connection"]["connect_timeout_sec"], 45.0)
        self.assertEqual(LLMClient.from_env()._settings.context_input_tokens, 64000)
        self.assertEqual(LLMClient.from_env()._settings.connect_retry_attempts, 4)
        self.assertNotIn('context_input_tokens', LLMClient.from_env()._build_request('system', 'user'))
        legacy_binding = json.loads(json.dumps(binding))
        legacy_binding['settings'].pop('context_input_tokens')
        legacy_binding['code_route']['connection'].pop('context_input_tokens')
        legacy_binding['settings'].pop('connect_retry_attempts')
        legacy_binding['code_route']['connection'].pop('connect_retry_attempts')
        self.assertTrue(model_connections_compatible(legacy_binding, tuned))
        self._write_catalog("coder-model", "next-model")
        self.assertNotEqual(LLMClient.from_env().connection_binding(), binding)
        self.assertFalse(model_connections_compatible(binding, LLMClient.from_env().connection_binding()))
        self._write_catalog('code = "coder"', 'code = "coder"\nwriting = "writer"')
        routed = LLMClient.from_env().connection_binding()
        self.assertNotIn("writer-secret", json.dumps(routed))
        self.assertFalse(model_connections_compatible(binding, routed))
        self._write_catalog('code = "coder"', 'code = "coder"\nwriting = "coder"')
        self.assertFalse(model_connections_compatible(routed, LLMClient.from_env().connection_binding()))
        writing_binding = LLMClient.from_env().connection_binding()
        self.assertNotIn("connect_timeout_sec", writing_binding["writing_route"]["connection"])
        # Isolate a connection-only retune across parent, code and writing routes.
        self.path.write_text(CATALOG.replace('code = "coder"', 'code = "coder"\nwriting = "coder"')
            .replace('retry_attempts = 1', 'retry_attempts = 1\nconnect_timeout_sec = 90.0'), encoding="utf-8")
        retuned = LLMClient.from_env()
        retuned_binding = retuned.connection_binding()
        self.assertEqual(retuned_binding["writing_route"]["connection"]["connect_timeout_sec"], 90.0)
        self.assertNotEqual(writing_binding, retuned_binding)
        self.assertTrue(model_connections_compatible(writing_binding, retuned_binding))
        self.assertTrue(model_connections_compatible(retuned_binding, writing_binding))
        writing_transport = json.loads(json.dumps(writing_binding))
        for settings in (writing_transport['settings'], writing_transport['code_route']['connection'],
                         writing_transport['writing_route']['connection']):
            settings['stream'] = not settings.get('stream', False)
        self.assertTrue(model_connections_compatible(writing_binding, writing_transport))
        self.assertEqual(LLMClient.for_task(client=retuned, purpose="writing")._settings.connect_timeout_sec, 90.0)
        self.path.write_text(self.path.read_text(encoding="utf-8").replace(
            "https://writer.example/v1", "https://other.example/v1"), encoding="utf-8")
        self.assertFalse(model_connections_compatible(retuned_binding, LLMClient.from_env().connection_binding()))

    def test_native_response_corrections_are_strict_chat_only_and_pinned(self):
        binding = LLMClient.from_env().connection_binding()
        self.assertNotIn('native_response_corrections', binding['settings'])
        old = json.loads(json.dumps(binding))
        old['settings']['native_response_corrections'] = 0
        old['code_route']['connection']['native_response_corrections'] = 0
        self.assertTrue(model_connections_compatible(old, binding))
        self.assertTrue(model_connections_compatible(binding, old))
        for reference in ('WRITER_KEY', 'CODER_KEY'):
            self._write_catalog(f'api_key_env = "{reference}"',
                f'api_key_env = "{reference}"\nnative_response_corrections = 1')
            client = LLMClient.from_env()
            self.assertFalse(model_connections_compatible(old, client.connection_binding()))
            self.assertFalse(model_connections_compatible(binding, client.connection_binding()))
            retuned = json.loads(json.dumps(client.connection_binding()))
            retuned['settings']['connect_timeout_sec'] = 90.0
            retuned['code_route']['connection']['connect_timeout_sec'] = 90.0
            self.assertFalse(model_connections_compatible(old, retuned))
            self.assertFalse(model_connections_compatible(retuned, old))
            if reference == 'WRITER_KEY':
                self.assertEqual(client._settings.native_response_corrections, 1)
                self.assertNotIn('native_response_corrections', client._build_request('system', 'user'))
        for value in ('true', 'false', '"1"', '1.0', '-1', '2'):
            with self.subTest(value=value):
                self._write_catalog('retry_attempts = 1', f'native_response_corrections = {value}', 1)
                with self.assertRaises(ModelConfigError):
                    load_model_catalog()
        for api in ('openai_responses', 'openai_images', 'cctq_images_async'):
            with self.subTest(api=api):
                original = 'api = "openai_images"' if 'images' in api else 'api = "openai_chat"'
                replacement = f'api = "{api}"\nnative_response_corrections = 1'
                self._write_catalog(original, replacement, 1)
                with self.assertRaises(ModelConfigError):
                    load_model_catalog()
        # Responses rejection above must not depend on the separate stream rule.
        self.path.write_text(CATALOG.replace('api = "openai_chat"', 'api = "openai_responses"', 1)
            .replace('stream = true', 'stream = false\nnative_response_corrections = 1'), encoding='utf-8')
        with self.assertRaises(ModelConfigError):
            load_model_catalog()

    def test_catalog_inspection_needs_no_credentials_and_images_are_not_text(self):
        catalog = load_model_catalog()
        self.assertEqual(catalog.select(purpose="image")[0], "artist")
        for api in ("openai_images", "cctq_images_async"):
            with self.subTest(api=api):
                self._write_catalog('api = "openai_images"', f'api = "{api}"')
                image_catalog = load_model_catalog()
                with self.assertRaises(ModelConfigError):
                    image_catalog.select("profile:artist", purpose="text")
                with self.assertRaises(ModelConfigError):
                    image_catalog.profiles["artist"].text_settings()
        self.path.write_text(CATALOG, encoding="utf-8")
        from simple_ar.cli.main import main
        output = io.StringIO()
        with patch("sys.argv", ["simple-ar", "models", "--config", str(self.path)]), contextlib.redirect_stdout(output):
            main()
        self.assertIn("artist", output.getvalue())
        self.assertNotIn("writer-secret", output.getvalue())
        for effort in ("", "none"):
            with self.subTest(thinking="disabled", effort=effort):
                self._write_catalog('api_key_env = "WRITER_KEY"',
                    'api_key_env = "WRITER_KEY"\nthinking_mode = "disabled"\n'
                    f'reasoning_effort = "{effort}"', 1)
                with patch.dict(os.environ, {}, clear=True):
                    profile = load_model_catalog(self.path).profiles["writer"]
                self.assertEqual(profile.thinking_mode, "disabled")
                self.assertEqual(profile.reasoning_effort, effort)

    def test_invalid_catalog_errors_do_not_echo_input(self):
        for original, replacement in [
            ('api_key_env = "WRITER_KEY"', 'api_key = "accidental-secret"'),
            ('https://writer.example/v1', 'https://accidental-secret@writer.example/v1'),
            ('retry_attempts = 1', 'retry_attempts = 0'),
            ('retry_attempts = 1', 'connect_retry_attempts = 0'),
            ('retry_attempts = 1', 'connect_retry_attempts = true'),
            ('api = "openai_images"', 'api = "openai_images"\nconnect_retry_attempts = 2'),
            ('api = "openai_images"', 'api = "openai_images"\nconnect_timeout_sec = 45.0'),
            ('api = "openai_images"', 'api = "cctq_images_async"\nconnect_timeout_sec = 45.0'),
            ('retry_attempts = 1', 'context_input_tokens = 0'),
            ('retry_attempts = 1', 'context_input_tokens = true'),
            ('api_key_env = "WRITER_KEY"', 'api_key_env = "WRITER_KEY"\nproxy_env = "http://accidental-secret@proxy"'),
            ('api = "openai_chat"', 'api = "openai_responses"'),
        ] + [
            ('retry_attempts = 1', f'connect_timeout_sec = {value}')
            for value in ('0', '-1', 'true', 'false', '"45"', 'inf', '-inf', 'nan')
        ] + [
            ('api_key_env = "WRITER_KEY"',
             'api_key_env = "WRITER_KEY"\nthinking_mode = "disabled"\n'
             f'reasoning_effort = "{effort}"')
            for effort in ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
        ]:
            with self.subTest(replacement=replacement):
                self._write_catalog(original, replacement, 1)
                with self.assertRaises(ModelConfigError) as caught:
                    load_model_catalog()
                self.assertNotIn("accidental-secret", str(caught.exception))

    def test_proxy_reference_validation_and_invalid_image_preflight_never_reserve(self):
        self.assertIsNone(load_model_catalog().profiles["artist"].resolve_proxy())
        self._write_catalog('api = "openai_images"', 'api = "openai_images"\nproxy_env = "IMAGE_PROXY"')
        catalog = load_model_catalog()
        profile = catalog.profiles["artist"]
        os.environ["IMAGE_KEY"] = "image-secret"
        for value in (None, "", "socks5://127.0.0.1:7890", "http://user:accidental-secret@proxy",
                      "http://@proxy", "http://proxy/?accidental-secret", "http://proxy/#accidental-secret",
                      "http://proxy:bad", "http://proxy:99999", "http://proxy\n"):
            with self.subTest(value=value):
                if value is None:
                    os.environ.pop("IMAGE_PROXY", None)
                else:
                    os.environ["IMAGE_PROXY"] = value
                with self.assertRaises(ModelConfigError) as caught:
                    profile.resolve_proxy()
                self.assertNotIn("accidental-secret", str(caught.exception))
                self._assert_image_preflight_rejected(catalog, "bad-proxy")
        for value in ("http://127.0.0.1:7890", "https://proxy.example:8443"):
            os.environ["IMAGE_PROXY"] = value
            self.assertEqual(profile.resolve_proxy(), value)

    def test_no_catalog_keeps_legacy_env(self):
        del os.environ["SIMPLE_AR_MODELS_CONFIG"]
        os.environ["SIMPLE_AR_LLM_CONNECT_TIMEOUT_SEC"] = "45"
        legacy = LLMClient.from_env()
        self.assertEqual(legacy.model, "legacy-model")
        self.assertIsNone(legacy._settings.connect_timeout_sec)
        self.assertEqual(legacy.connection_binding(), {})
        with self.assertRaises(LLMError):
            LLMClient.from_env(model="profile:writer")

    def test_retained_catalog_locations_are_hints_not_new_connections(self):
        from simple_ar.integrations.model_profiles import catalog_path

        original = LLMClient.from_env('profile:writer').connection_binding()
        hint = str(self.path.resolve())
        os.environ.pop('SIMPLE_AR_MODELS_CONFIG')
        # A process-local environment selection need not survive restart. No
        # nearby project configuration is scanned or merged with this file.
        self.assertEqual(LLMClient.from_env('profile:writer', fallback_models_config=hint).connection_binding(), original)
        self.assertEqual(catalog_path(fallback_path=hint), self.path)
        global_path = self.root / '.config/simple-ar/models.toml'
        global_path.parent.mkdir(parents=True)
        global_path.write_text(CATALOG.replace('writer-model', 'another-model'), encoding='utf-8')
        self.assertEqual(LLMClient.from_env('profile:writer', fallback_models_config=hint).model, 'writer-model')
        # Explicit caller/environment choices still win. The task owner, not
        # the locator, must reject incompatible receivers before any dispatch.
        os.environ['SIMPLE_AR_MODELS_CONFIG'] = str(global_path)
        self.assertEqual(LLMClient.from_env('profile:writer', fallback_models_config=hint).model, 'another-model')
        self.assertEqual(LLMClient.from_env('profile:writer', models_config=hint,
            fallback_models_config=str(global_path)).model, 'writer-model')
        os.environ['SIMPLE_AR_MODELS_CONFIG'] = str(self.root / 'absent-explicit.toml')
        with self.assertRaisesRegex(LLMError, 'does not exist'):
            LLMClient.from_env('profile:writer', fallback_models_config=hint)
        os.environ.pop('SIMPLE_AR_MODELS_CONFIG')
        with self.assertRaisesRegex(LLMError, 'absolute'):
            LLMClient.from_env('profile:writer', fallback_models_config='models.toml')
        self.path.unlink()
        self.assertEqual(LLMClient.from_env('profile:writer', fallback_models_config=hint).model, 'another-model')
        self.path.write_text('api_key = "do-not-echo-this-value"\n', encoding='utf-8')
        with self.assertRaises(LLMError) as caught:
            LLMClient.from_env('profile:writer', fallback_models_config=hint)
        self.assertNotIn('do-not-echo-this-value', str(caught.exception))
        global_path.unlink()
        self.path.unlink()
        with self.assertRaisesRegex(LLMError, 'Named models require'):
            LLMClient.from_env('profile:writer', fallback_models_config=hint)

    def test_saved_session_rejects_connection_drift_before_execution(self):
        from simple_ar.app.research_application import (
            create_session, load_session, ResearchApplicationServices, ResearchApplicationError)
        from simple_ar.research.workflow_contracts import ResearchBrief
        root = self.root / "session"
        create_session(ResearchBrief(request_text="Compare supplied evidence", requested_outputs=("summary",)),
                       root=root, services=ResearchApplicationServices(llm_client=LLMClient.from_env()))
        os.environ["WRITER_KEY"] = "rotated"
        restored = load_session(root, services=ResearchApplicationServices(llm_client=LLMClient.from_env()))
        restored.require_llm_binding()
        self._write_catalog("writer-model", "changed-model")
        changed = load_session(root, services=ResearchApplicationServices(llm_client=LLMClient.from_env()))
        with self.assertRaisesRegex(ResearchApplicationError, "connections differ"):
            changed.require_llm_binding()

    def test_explicit_code_connection_survives_nested_task_clients(self):
        self._write_catalog('code = "coder"', 'code = "writer"')
        chosen = LLMClient.from_env(model="profile:coder", purpose="code")
        self.assertEqual(LLMClient.for_task(client=chosen, model=chosen.model).model, "coder-model")

    def test_http2_is_opt_in_transport_not_provider_payload(self):
        original_binding = LLMClient.from_env().connection_binding()
        self._write_catalog('stream = true', 'stream = true\nhttp2 = true')
        client = LLMClient.from_env()
        self.assertNotEqual(client.connection_binding(), original_binding)
        with patch("simple_ar.integrations.llm._call_openai_chat_stream", new_callable=AsyncMock) as transport:
            transport.return_value = {"choices": [{"message": {"content": "ok"}}]}
            self.assertEqual(client.ask("system", "user"), "ok")
            self.assertTrue(transport.call_args.kwargs["http2"])
            self.assertNotIn("http2", transport.call_args.args[0])
            transport.assert_awaited_once()
        with patch("openai.DefaultAsyncHttpxClient", side_effect=ImportError), patch("openai.AsyncOpenAI") as factory:
            with self.assertRaisesRegex(LLMError, "optional dependency"):
                client.ask("system", "user")
            factory.assert_not_called()
