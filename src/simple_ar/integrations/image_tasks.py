"""Explicit CCTQ async protocol; caller owns accounting and serializes this store.

A started record without an ID is never resubmitted. With an ID, every recovery
polls the same task, even after completion/download failure. No synchronous
result directory is adopted. Image validation and final delivery belong to caller.
"""
from __future__ import annotations

import asyncio
import logging
import math
import mimetypes
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

import httpx

from simple_ar.core.capabilities import ArtifactStore


class ImageTaskError(RuntimeError):
    """Safe public diagnostic; never includes provider bodies or credentials."""


class ImageTaskInterrupted(ImageTaskError):
    """Submission may have reached the provider, but its ID was not persisted."""


def _numeric_usage(value: object, key: str) -> dict:
    if not isinstance(value, dict):
        return {}
    return {name: _numeric_usage(item, key) if isinstance(item, dict) else item
            for name, item in value.items()
            if isinstance(name, str) and re.fullmatch(r"[a-z_]+", name) and key not in name
            and (isinstance(item, dict) or
                 (type(item) in (int, float) and math.isfinite(item)))}


def request_task_image(*, base_url: str, key: str, model: str, prompt: str,
                       source: Path | None, options: dict, timeout: float,
                       http2: bool, store: ArtifactStore, proxy: str | None = None) -> tuple[bytes, dict, str]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_request_task_image(base_url=base_url, key=key, model=model,
            prompt=prompt, source=source, options=options, timeout=timeout,
            http2=http2, store=store, proxy=proxy))
    raise ImageTaskError("Synchronous image requests cannot run inside an asyncio loop; use asyncio.to_thread")


async def _request_task_image(*, base_url: str, key: str, model: str, prompt: str,
                              source: Path | None, options: dict, timeout: float,
                              http2: bool, store: ArtifactStore,
                              proxy: str | None = None) -> tuple[bytes, dict, str]:
    """One POST at most; retries after a saved ID issue GETs only.

    The timeout bounds this invocation, including submission, polling and download.
    Returned usage is numeric provider usage, not a price or estimated token bill.
    Keys may rotate during recovery; the nonsecret request fingerprint must match.
    """
    levels = {}
    client = deadline_scope = None
    try:
        base = base_url.rstrip("/")
        parsed = urlsplit(base)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or "%" in parsed.path or "\\" in base
                or any(ord(c) <= 32 for c in base)):
            raise ImageTaskError("Use a credential-free HTTPS API base URL without query or fragment")
        if not key or not model or not prompt.strip() or not math.isfinite(timeout) or timeout <= 0:
            raise ImageTaskError("Supply credentials, model, prompt and a positive finite timeout")
        if set(options) - {"size", "quality"}:
            raise ImageTaskError("Async image options support explicit size and quality only")
        # The caller owns the immutable request/input identity. Keep the plain
        # protocol parameters here rather than introducing another hash scheme.
        fingerprint = {"base_url": base.replace(key, "[REDACTED]"), "model": model.replace(key, "[REDACTED]"),
                       "prompt": prompt.replace(key, "[REDACTED]"), "options": options}
        path = "image_task.json"
        state = store.read_json(path) if store.exists(path) else None
        if state is not None:
            if not isinstance(state, dict) or state.get("fingerprint") != fingerprint:
                raise ImageTaskError("Saved async request differs; use a new independent version")
            if not state.get("id"):
                raise ImageTaskInterrupted("Interrupted submission without saved task ID; do not resubmit")
        else:
            allowed = {"request.json", "budget.json"}
            if store.exists("request.json"):
                if store.read_json("request.json").get("api") != "cctq_images_async":
                    raise ImageTaskError("A synchronous request cannot be adopted as an async task")
            if store.root.exists() and any(p.name not in allowed for p in store.root.iterdir()):
                raise ImageTaskError("Use an empty async version; synchronous records cannot be adopted")
            state = {"fingerprint": fingerprint, "phase": "started"}
            store.write_json(path, state)  # Persist BEFORE entering the POST boundary.
        deadline = time.monotonic() + timeout

        def remaining(cap: float = 30) -> float:
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise ImageTaskError("Async image deadline exceeded; resume saved ID with GET only")
            return min(cap, seconds)

        def task(payload: object, expected: str | None = None) -> tuple[str, str]:
            if not isinstance(payload, dict) or payload.get("object") != "image.task":
                raise ImageTaskError("Invalid image task envelope; retain record and inspect provider contract")
            tid, status = payload.get("id"), payload.get("status")
            if (not isinstance(tid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", tid)
                    or key in tid or (expected is not None and tid != expected)
                    or status not in {"queued", "in_progress", "completed", "failed"}):
                raise ImageTaskError("Invalid image task identity or status; no resubmission")
            return tid, status

        # httpx DEBUG includes URLs; suppress transport logs while credentials are used.
        for name in {"httpx", "httpcore", *[n for n in logging.root.manager.loggerDict
                                               if n.startswith(("httpx.", "httpcore."))]}:
            levels[name] = logging.getLogger(name).level
            logging.getLogger(name).setLevel(logging.CRITICAL + 1)
        # Match the SDK transport: honor the caller's configured HTTP(S) proxy
        # and certificate environment, with normal TLS verification. This is
        # network routing, not an automatic provider/base-URL fallback.
        client = httpx.AsyncClient(http2=http2, follow_redirects=False,
            **({"proxy": proxy} if proxy else {}), headers={"Authorization": f"Bearer {key}"})
        deadline_scope = asyncio.timeout(timeout)
        async with deadline_scope:
            if "id" not in state:
                fields = {**options, "model": model, "prompt": prompt, "async": True}
                if source is None:
                    response = await client.post(base + "/images/generations", json=fields,
                                           timeout=remaining(timeout))
                else:
                    with source.open("rb") as handle:
                        mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
                        response = await client.post(base + "/images/edits",
                            data={k: "true" if v is True else str(v) for k, v in fields.items()},
                            files={"image": (source.name, handle, mime)}, timeout=remaining(timeout))
                if response.status_code != 202:
                    raise ImageTaskError("Async image submission did not return 202; do not resubmit blindly")
                tid, status = task(response.json())
                state.update(id=tid, status=status, phase="accepted")
                store.write_json(path, state)  # Save ID BEFORE any GET.
            tid, _ = task({"object": "image.task", **state})
            endpoint = base + "/images/tasks/" + tid
            # Completion is already recorded by this provider; recovery only
            # needs the authenticated file, not another task-status request.
            while state.get("status") != "completed":
                response = await client.get(endpoint, timeout=remaining(timeout))
                if response.status_code != 200:
                    raise ImageTaskError("Image task query failed; resume saved ID, do not resubmit")
                payload = response.json()
                _, status = task(payload, tid)
                state["status"] = status
                if status == "completed":
                    result = payload.get("result")
                    if not isinstance(result, dict):
                        raise ImageTaskError("Completed image task lacks result; retain saved ID")
                    state["usage"] = _numeric_usage(result.get("usage"), key)
                store.write_json(path, state)  # Never persist result URLs or error bodies.
                if status == "failed":
                    raise ImageTaskError("Provider image task failed; saved ID retained, no automatic new task")
                if status == "completed":
                    break
                await asyncio.sleep(min(5, remaining()))
            content = bytearray()
            async with client.stream("GET", endpoint + "/files/0", timeout=remaining(timeout)) as response:
                if response.status_code != 200:
                    raise ImageTaskError("Image task download failed; resume saved ID with GET only")
                async for chunk in response.aiter_bytes():
                    remaining()
                    if len(content) + len(chunk) > 20 * 1024 * 1024:
                        raise ImageTaskError("Image task download exceeds 20 MiB; saved ID retained")
                    content.extend(chunk)
            if not content:
                raise ImageTaskError("Image task download is empty; saved ID retained")
            return bytes(content), state["usage"], tid
    except ImageTaskError:
        raise
    except Exception as error:
        if isinstance(error, TimeoutError) and deadline_scope is not None and deadline_scope.expired():
            raise ImageTaskError("Async image deadline exceeded; retain saved state, resume a saved ID with GET only") from None
        raise ImageTaskError(f"Async image transport or record failure ({type(error).__name__}); inspect saved state, never resend blindly") from None
    finally:
        if client is not None:
            try:
                async with asyncio.timeout(2.0):
                    await client.aclose()
            except Exception:
                pass  # Cleanup must not replace the request result or failure.
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)
