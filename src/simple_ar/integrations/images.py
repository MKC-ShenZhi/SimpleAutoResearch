"""One Images submission per caller-owned directory; no automatic resubmissions.

The controller supplies a persistent ledger, explicit reserves (image_requests=1,
plus any authorized cost/token bounds), and a separate store for each version.
Replaying an identical saved request restores its result, never resends it.
"""
from __future__ import annotations

import base64
from contextlib import ExitStack, contextmanager
import hashlib
from importlib import import_module
from io import BytesIO
import json
import logging
import time
from typing import Any, Mapping

from PIL import Image
from openai import DefaultHttpxClient, OpenAI

from simple_ar.core.budget import BudgetLedger, BudgetNumber
from simple_ar.core.capabilities import ArtifactRef, ArtifactStore, CapabilityResult
from simple_ar.integrations.model_profiles import ModelCatalog
from simple_ar.integrations.image_tasks import ImageTaskError


class ImagesError(RuntimeError):
    """Public errors never include SDK exception text or response bodies."""


@contextmanager
def _quiet_sdk():
    # SDK debug logs include request bodies and headers. Restore caller levels.
    names = {"openai", "httpx", "httpcore"} | {
        name for name in logging.root.manager.loggerDict
        if name.startswith(("openai.", "httpx.", "httpcore."))
    }
    levels = {name: logging.getLogger(name).level for name in names}
    try:
        for name in names:
            logging.getLogger(name).setLevel(logging.CRITICAL + 1)
        yield
    finally:
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)


def _usage(value: Any) -> dict[str, Any]:
    """Keep measured numeric SDK usage only, including nested token details."""
    raw = value.model_dump(mode="json") if value is not None else {}
    return {k: _usage_dict(v) for k, v in raw.items()
            if isinstance(v, (dict, int, float)) and not isinstance(v, bool)}


def _usage_dict(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return {k: _usage_dict(v) for k, v in value.items()
            if isinstance(v, (dict, int, float)) and not isinstance(v, bool)}


def _account(ledger: BudgetLedger, rid: str, usage: dict[str, Any]) -> None:
    entry = next(e for e in ledger.entries if e.reservation_id == rid)
    if entry.status != "reserved":
        return
    actual = {"image_requests": usage["image_requests"]}
    actual.update({k: v for k, v in usage.get("provider_usage", {}).items()
                   if k in {"input_tokens", "output_tokens", "total_tokens"}})
    if set(entry.reserved) <= actual.keys():
        ledger.settle(rid, actual, actual_source="provider",
                      provider_call_id=usage.get("request_id", ""))
    else:
        ledger.mark_unknown(rid, reason="Images cost/usage not reported; no estimate",
                            known_actual=actual)


def run_image(
    catalog: ModelCatalog, prompt: str, *, store: ArtifactStore,
    ledger: BudgetLedger, reservation_id: str,
    reserves: Mapping[str, BudgetNumber], selector: str | None = None,
    input_ref: ArtifactRef | None = None, input_store: ArtifactStore | None = None,
) -> CapabilityResult:
    """Generate one image, or edit a registered local image into a new store.

    Returned image/request references are relative to ``store``. Editing records
    the original input reference, source root and hash in request.json. A failed
    or interrupted directory must be inspected; a new attempt needs a new ID/root.
    Callers serialize access to the store/ledger (and SDK logging configuration).
    """
    entered = False
    resuming = False
    started = time.monotonic()
    entry = None
    diagnostic = "Images preflight/recovery failed; check profile, input, budget and saved directory"
    usage: dict[str, Any] = {"image_requests": 0, "provider_usage": {}, "cost": None}
    request_ref = store.ref("request.json", kind="image_request", producer="images")
    try:
        name, profile = catalog.select(selector, purpose="image")
        if input_ref is not None and "image_edit" not in profile.capabilities:
            raise ImagesError("Selected image profile must declare image_edit")
        if ledger.storage_path is None or ledger.limits.get("image_requests") is None:
            raise ImagesError("Supply a persistent ledger with a finite image_requests limit")
        if reserves.get("image_requests") != 1 or not prompt.strip():
            raise ImagesError("Supply a nonempty prompt and image_requests=1 reserve")
        key = profile.credential()
        proxy = profile.resolve_proxy()
        source = (input_store or store).require(input_ref) if input_ref else None
        if source is not None:
            if input_ref.status != "available" or source.resolve().is_relative_to(store.root.resolve()):
                raise ImagesError("Edit requires an available input in a separate source store")
            with Image.open(source) as image:
                image.verify()
        options = {k: v for k, v in {"size": profile.image_size,
                                    "quality": profile.image_quality}.items() if v is not None}
        request = {"operation": "edit" if source else "generate", "profile": name,
                   "base_url": profile.base_url, "model": profile.model, "prompt": prompt,
                   "options": options, "reservation_id": reservation_id, "reserves": dict(reserves),
                   "timeout": profile.request_timeout_sec, "http2": profile.http2,
                   "input": {"ref": input_ref.to_dict(), "root": str((input_store or store).root),
                             "sha256": hashlib.sha256(source.read_bytes()).hexdigest()} if source else None}
        if profile.api == "cctq_images_async":
            request["api"] = profile.api
        # Never persist a credential accidentally included in caller/provider strings.
        request = json.loads(json.dumps(request).replace(json.dumps(key)[1:-1], "[REDACTED]"))
        if store.exists(request_ref):
            saved = store.read_json(request_ref)
            task = store.read_json("image_task.json") if store.exists("image_task.json") else {}
            # A known async task can change transport settings without changing
            # the already submitted generation. Keep its original request intact.
            transport_only = profile.api == "cctq_images_async" and bool(task.get("id"))
            compared = {k: v for k, v in request.items() if not transport_only or k not in {"timeout", "http2"}}
            original = {k: v for k, v in saved.items() if not transport_only or k not in {"timeout", "http2"}}
            if original != compared:
                raise ImagesError("Saved image request differs; use a new output directory and ID")
            result = store.read_capability_result() if store.exists("capability_result.json") else None
            if result is not None and result.status == "completed":
                for ref in result.artifacts:
                    if ref.kind == "image":
                        with Image.open(store.require(ref)) as image:
                            image.verify()
                _account(ledger, reservation_id, result.usage)
                return result
            resuming = profile.api == "cctq_images_async" and bool(task.get("id")) and task.get("status") != "failed"
            if not resuming:
                if result is not None:
                    return result
                raise ImagesError("Interrupted Images request without a saved task ID; inspect ledger, do not resend")
            entry = next((e for e in ledger.entries if e.reservation_id == reservation_id), None)
            if entry is None or entry.status != "reserved":
                raise ImagesError("Saved image task requires its original pending reservation")
        if not resuming and any(e.reservation_id == reservation_id for e in ledger.entries):
            raise ImagesError("Reservation already exists; recover its original directory")
        # Validate the optional transport before reserving or recording a
        # submission. A missing local dependency is not an unknown POST.
        # Completed, intact artifacts returned above need no network runtime.
        if profile.http2:
            try:
                import_module("h2.connection")
            except ImportError:
                raise ImagesError("HTTP/2 requires simple-autoresearch[http2]; no image submission attempted") from None
        store.root.mkdir(parents=True, exist_ok=True)
        if not resuming and any(store.root.iterdir()):
            raise ImagesError("Images requires an empty, independent output directory")
        if not resuming:
            entry = ledger.reserve(reservation_id, reserves, attempt_id=store.root.name,
                                   logical_call_id=reservation_id, purpose="image")
            store.write_json(request_ref.path, request, kind="image_request", producer="images")
        diagnostic = "Images request failed; check profile/credential/dependency and use a new attempt"
        if profile.api == "cctq_images_async":
            from simple_ar.integrations.image_tasks import request_task_image
            entered = True
            with _quiet_sdk():
                content, provider_usage, task_id = request_task_image(base_url=profile.base_url,
                    key=key, model=profile.model, prompt=prompt, source=source, options=options,
                    timeout=profile.request_timeout_sec, http2=profile.http2, store=store,
                    **({"proxy": proxy} if proxy else {}))
            usage = {"image_requests": 1, "provider_usage": provider_usage,
                     "request_id": task_id, "cost": None}
        else:
            with _quiet_sdk(), ExitStack() as stack:
                http = stack.enter_context(DefaultHttpxClient(http2=profile.http2, **({"proxy": proxy} if proxy else {})))
                client = stack.enter_context(OpenAI(api_key=key, base_url=profile.base_url,
                                                   timeout=profile.request_timeout_sec,
                                                   max_retries=0, http_client=http))
                if source:
                    handle = stack.enter_context(source.open("rb"))
                    entered = True
                    response = client.images.edit(model=profile.model, image=[handle], prompt=prompt, **options)
                else:
                    entered = True
                    response = client.images.generate(model=profile.model, prompt=prompt, n=1, **options)
            usage = {"image_requests": 1, "provider_usage": _usage(getattr(response, "usage", None)),
                     "request_id": str(getattr(response, "_request_id", "") or "").replace(key, "[REDACTED]"),
                     "cost": None}
            item = response.data[0] if response.data else None
            if item is None or not item.b64_json:
                diagnostic = "Provider returned no b64_json; configure base64 image output or select a compatible profile; arbitrary URL download is unsupported"
                raise ImagesError(diagnostic)
            content = base64.b64decode(item.b64_json, validate=True)
        usage = json.loads(json.dumps(usage).replace(json.dumps(key)[1:-1], "[REDACTED]"))
        diagnostic = "Image payload invalid; inspect saved request and use a new attempt"
        with Image.open(BytesIO(content)) as image:
            image.verify()
            suffix = Image.registered_extensions()
            extension = next(ext for ext, fmt in suffix.items() if fmt == image.format)
        output = store.resolve("image" + extension)
        temporary = store.resolve("image.pending")
        with temporary.open("xb") as stream:
            stream.write(content)
        temporary.replace(output)
        result = CapabilityResult("completed", (store.ref(output, kind="image", producer="images"), request_ref),
                                  usage=usage, provenance={"profile": request["profile"], "model": request["model"]})
        store.write_capability_result(result)
        _account(ledger, reservation_id, usage)
        return result
    except Exception as exc:
        if isinstance(exc, (ImagesError, ImageTaskError)):
            diagnostic = str(exc)
        elif entered:
            status = getattr(exc, "status_code", None)
            code = f", HTTP {status}" if type(status) is int else ""
            diagnostic = f"Images request failed ({type(exc).__name__}{code}); response body omitted"
        usage["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if entry is None:
            raise ImagesError(diagnostic) from None
        try:
            task = store.read_json("image_task.json") if store.exists("image_task.json") else {}
            pending_task = profile.api == "cctq_images_async" and bool(task.get("id")) and task.get("status") != "failed"
            if store.exists("capability_result.json") and not resuming:
                raise ImagesError("Saved result retained; reconcile ledger before continuing")
            if pending_task:
                usage["image_requests"] = 1
                usage["provider_usage"] = task.get("usage", {})
                usage["request_id"] = task["id"]
                diagnostic += "; saved task ID retained: repeat the identical command to query/download only, never resubmit"
            elif entry.status == "reserved":
                if usage["image_requests"] == 1:
                    _account(ledger, reservation_id, usage)
                elif entered:
                    usage["image_requests"] = 1
                    ledger.mark_unknown(reservation_id, reason="Images failed after dispatch; do not retry",
                                        known_actual={"image_requests": 1})
                else:
                    ledger.release(reservation_id, reason="Images preflight failed before dispatch")
            result = CapabilityResult("failed", (request_ref,) if store.exists(request_ref) else (),
                                      diagnostics=(diagnostic,), usage=usage)
            store.write_capability_result(result)
            return result
        except Exception:
            raise ImagesError("Images record/accounting failed; inspect saved directory and ledger; do not resend") from None
