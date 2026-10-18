"""Versioned image tools over the existing submission/recovery owner."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from simple_ar.core.capabilities import ArtifactStore
from simple_ar.integrations.images import ImagesError, run_image
from simple_ar.integrations.model_profiles import load_model_catalog
from simple_ar.tools.specs import CommonToolSpec, ToolResult


class _Image(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    prompt: str = Field(min_length=1, max_length=32000)
    input_image: str = Field(default="", description="Mounted image to edit; empty generates a new image. Never overwrites the input.")


class _Resume(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: str = Field(description="Exact version returned by create_image; resumes the saved request, never starts a replacement.")


def image_tool_specs() -> list[CommonToolSpec]:
    return [CommonToolSpec(name=name, domain="images", permission_level="network", risk_level="medium",
        input_schema=model.model_json_schema(), description=description)
        for name, model, description in (
            ("create_image", _Image, "Generate a concept illustration or edit a mounted image through the named image route. Saves a new version and uses the shared image allowance; not measured data or editable vector artwork."),
            ("resume_image", _Resume, "Recover a previously saved image version. Known async tasks are queried/downloaded without a new submission; unknown dispatches are not retried."))]


class ImageToolGateway:
    def can_reconcile(self, name: str, identity: str) -> bool:
        # resume_image already targets an original version and rejects absent
        # reservations. create_image must never reach a new submission here.
        return name == 'resume_image' or (name == 'create_image'
            and (self.workspace.workspace / 'image-versions' / identity / 'result' / 'request.json').is_file()
            and any(entry.reservation_id == f'image:{identity}' for entry in self.ledger.entries))

    def __init__(self, workspace: Any, ledger: Any, *, fallback_models_config: str | None = None) -> None:
        self.workspace, self.ledger = workspace, ledger
        self.fallback_models_config = fallback_models_config

    def call(self, name: str, arguments: dict[str, Any], *, identity: str) -> ToolResult:
        version = ""
        try:
            if name not in {"create_image", "resume_image"}:
                raise ValueError("Unknown image tool")
            request = (_Image if name == "create_image" else _Resume).model_validate(arguments)
            version = identity if name == "create_image" else request.version
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", version):
                raise ValueError("Invalid image version")
            prefix = f"workspace/image-versions/{version}"
            directory = self.workspace.resolve(prefix, write=True)
            owner = ArtifactStore(directory)
            if name == "create_image" and any(entry.reservation_id.startswith("image:")
                    and entry.reservation_id != f"image:{version}" and entry.status in {"reserved", "unknown"}
                    for entry in self.ledger.entries):
                raise ValueError("Recover the pending image version before any new submission")
            catalog = load_model_catalog(fallback_path=self.fallback_models_config)
            if catalog is None:
                raise ValueError("Configure a named image route")
            if name == "create_image":
                profile, connection = catalog.select(purpose="image")
                connection.credential()
                connection.resolve_proxy()
                operation = {"prompt": request.prompt, "input_image": request.input_image, "profile": profile}
                if request.input_image:
                    source = self.workspace.resolve(request.input_image)
                    if not source.is_file() or source.stat().st_size > 20 * 1024 * 1024:
                        raise ValueError("Use an existing image no larger than 20 MiB")
                if owner.exists("operation.json"):
                    if owner.read_json("operation.json") != operation:
                        raise ValueError("Image version inputs changed")
                else:
                    owner.write_json("operation.json", operation)
            else:
                operation = owner.read_json("operation.json")
                if not any(entry.reservation_id == f"image:{version}" for entry in self.ledger.entries):
                    raise ValueError("Image version has no original reservation; cannot submit on resume")
            source = self.workspace.resolve(operation["input_image"]) if operation["input_image"] else None
            if source and (not source.is_file() or source.stat().st_size > 20 * 1024 * 1024):
                raise ValueError("Use an existing image no larger than 20 MiB")
            input_store = ArtifactStore(source.parent) if source else None
            result = run_image(catalog, operation["prompt"],
                store=ArtifactStore(directory / "result"), ledger=self.ledger,
                reservation_id=f"image:{version}", reserves={"image_requests": 1},
                selector=f"profile:{operation['profile']}", input_store=input_store,
                input_ref=input_store.ref(source.name, kind="image") if source else None)
            paths = [f"{prefix}/result/{ref.path}" for ref in result.artifacts]
            return ToolResult(tool_name=name, status="ok" if result.status == "completed" else "error",
                data={"version": version, "status": result.status,
                    "artifacts": paths, "diagnostics": list(result.diagnostics),
                    "usage": result.usage, "provenance": result.provenance,
                    "scope": "Generated illustration; not measured scientific evidence or editable vector source"},
                artifacts=paths)
        except ImagesError as exc:
            # This adapter deliberately exposes only redacted public diagnostics.
            return ToolResult(tool_name=name, status="error", error=str(exc),
                data={"version": version, "status": "failed", "automatic_resubmission": False})
        except Exception as exc:
            return ToolResult(tool_name=name, status="error",
                error=f"Image operation unavailable ({type(exc).__name__}); retain its version and ledger, inspect the named image route. Resolve pending/unknown versions before new submissions.",
                data={"version": version, "status": "failed", "automatic_resubmission": False})
