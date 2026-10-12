"""Named, non-secret connection catalog. No network, environment mutation or dispatch."""
from __future__ import annotations

import os
from pathlib import Path
import re
import tomllib
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ModelConfigError(ValueError):
    pass


def model_connections_compatible(saved: dict, current: dict) -> bool:
    """Keep model/endpoint identity pinned, but allow operational retuning.

    Full settings remain in snapshots; comparison also handles older snapshots
    without rewriting their history. Output caps may change completion length,
    never the identity of the model used for the saved scientific task.
    """
    operational = {"request_timeout_sec", "max_output_tokens", "retry_attempts",
                   "retry_base_delay_sec", "retry_max_delay_sec", "context_input_tokens"}

    def identity(value):
        if isinstance(value, dict):
            return {key: identity(item) for key, item in value.items() if key not in operational}
        return value

    return identity(saved) == identity(current)


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    api: Literal["openai_chat", "openai_responses", "openai_images", "cctq_images_async"]
    base_url: str
    model: str = Field(min_length=1)
    api_key_env: str
    proxy_env: str = ""
    capabilities: list[Literal["text", "code", "vision", "image_generate", "image_edit"]]
    stream: bool = False
    http2: bool = False
    request_timeout_sec: float = Field(default=180.0, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    context_input_tokens: int = Field(default=24000, gt=0)
    retry_attempts: int = Field(default=3, ge=1)
    retry_base_delay_sec: float = Field(default=1.0, gt=0)
    retry_max_delay_sec: float = Field(default=12.0, gt=0)
    reasoning_effort: Literal["", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"] = ""
    thinking_mode: Literal["", "enabled", "disabled", "auto"] = ""
    reasoning_output_tokens: int | None = Field(default=None, gt=0)
    json_response_format: Literal["off", "auto", "json_object"] = "auto"
    chat_token_limit_param: Literal["auto", "max_tokens", "max_completion_tokens"] = "auto"
    input_price_per_million: float | None = Field(default=None, ge=0)
    output_price_per_million: float | None = Field(default=None, ge=0)
    image_size: Literal["auto", "256x256", "512x512", "1024x1024", "1024x1536", "1536x1024", "1792x1024", "1024x1792"] | None = None
    image_quality: Literal["auto", "standard", "hd", "low", "medium", "high"] | None = None

    @field_validator("base_url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Use an HTTP(S) base URL without credentials, query or fragment")
        return value.rstrip("/")

    @field_validator("api_key_env")
    @classmethod
    def key_reference(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("api_key_env must name an environment variable, not contain a key")
        return value

    @field_validator("proxy_env")
    @classmethod
    def proxy_reference(cls, value: str) -> str:
        return cls.key_reference(value) if value else value

    @model_validator(mode="after")
    def compatible(self) -> "ModelProfile":
        if (self.api == "openai_chat" and self.thinking_mode == "disabled"
                and self.reasoning_effort not in {"", "none"}):
            raise ValueError("thinking_mode='disabled' requires reasoning_effort='' or 'none'")
        caps = set(self.capabilities)
        if not caps:
            raise ValueError("Declare at least one capability")
        if self.api in {"openai_images", "cctq_images_async"}:
            if caps - {"image_generate", "image_edit"}:
                raise ValueError("Images profiles only declare image capabilities")
            text_options = {"stream", "max_output_tokens", "context_input_tokens", "reasoning_effort", "json_response_format",
                            "chat_token_limit_param", "thinking_mode", "reasoning_output_tokens",
                            "input_price_per_million", "output_price_per_million"}
            if self.model_fields_set & text_options:
                raise ValueError("Do not put text-generation options in an Images profile")
        elif caps & {"image_generate", "image_edit"}:
            raise ValueError("Image generation requires an Images profile")
        elif self.model_fields_set & {"image_size", "image_quality"}:
            raise ValueError("Image size/quality require an Images profile")
        elif self.api == "openai_responses" and self.stream:
            raise ValueError("This client currently streams Chat only; use stream=false for Responses")
        return self

    def credential(self) -> str:
        value = os.environ.get(self.api_key_env, "")
        if not value.strip():
            raise ModelConfigError(f"Missing credential environment variable: {self.api_key_env}")
        return value

    def resolve_proxy(self) -> str | None:
        """Resolve transport-only configuration; never persist or echo its value."""
        return resolve_proxy_env(self.proxy_env)

    def text_settings(self) -> dict:
        if self.api in {"openai_images", "cctq_images_async"}:
            raise ModelConfigError("An Images connection cannot be used by the text client")
        values = self.model_dump(exclude={"api", "api_key_env", "capabilities", "image_size", "image_quality"})
        return {**values, "api_key": self.credential(),
                "api_mode": "chat" if self.api == "openai_chat" else "responses"}


def resolve_proxy_env(reference: str) -> str | None:
    if not reference:
        return None
    try:
        ModelProfile.key_reference(reference)
    except (TypeError, ValueError):
        raise ModelConfigError("Invalid proxy environment variable reference") from None
    value = os.environ.get(reference, "")
    if not value.strip():
        raise ModelConfigError(f"Missing proxy environment variable: {reference}")
    try:
        parsed = urlsplit(value)
        parsed.port  # Validate malformed/out-of-range ports without exposing input.
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or "?" in value or "#" in value or "\\" in value
                or any(ord(char) <= 32 for char in value)):
            raise ValueError
    except ValueError:
        raise ModelConfigError("Proxy must be an HTTP(S) URL without userinfo, query or fragment") from None
    return value


class ModelCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1
    profiles: dict[str, ModelProfile]
    routes: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def references(self) -> "ModelCatalog":
        if not self.profiles or any(not name.strip() or ":" in name for name in self.profiles):
            raise ValueError("Profiles need nonempty names without ':'")
        if set(self.routes) - {"default", "text", "code", "writing", "vision", "image"}:
            raise ValueError("Unknown route; use default, text, code, writing, vision or image")
        if set(self.routes.values()) - set(self.profiles):
            raise ValueError("Route refers to an unknown profile")
        return self

    def select(self, selector: str | None = None, *, purpose: str = "text") -> tuple[str, ModelProfile]:
        if selector and selector.startswith("profile:"):
            name = selector.removeprefix("profile:")
        else:
            route = selector.removeprefix("route:") if selector and selector.startswith("route:") else purpose
            if route not in {"default", "text", "code", "writing", "vision", "image"}:
                raise ModelConfigError("Unknown model route")
            name = self.routes.get(route) or self.routes.get("default", "")
        if name not in self.profiles:
            raise ModelConfigError("Model profile/route is missing; configure it explicitly")
        profile = self.profiles[name]
        required = {"image": "image_generate", "writing": "text"}.get(purpose, purpose)
        if required not in profile.capabilities:
            raise ModelConfigError(f"Profile {name!r} does not declare capability {required!r}")
        if selector and not selector.startswith(("profile:", "route:")) and selector != profile.model:
            raise ModelConfigError("A raw model override cannot change a named connection; select profile:NAME")
        return name, profile


def catalog_path(path: str | Path | None = None) -> Path | None:
    selected = path or os.environ.get("SIMPLE_AR_MODELS_CONFIG")
    if selected:
        candidate = Path(selected).expanduser()
        if not candidate.is_file():
            raise ModelConfigError("The configured model catalog does not exist")
        return candidate
    candidate = Path.home() / ".config" / "simple-ar" / "models.toml"
    return candidate if candidate.is_file() else None


def load_model_catalog(path: str | Path | None = None) -> ModelCatalog | None:
    candidate = catalog_path(path)
    if candidate is None:
        return None
    try:
        with candidate.open("rb") as stream:
            return ModelCatalog.model_validate(tomllib.load(stream))
    except (OSError, ValueError) as exc:
        # ValidationError normally echoes input values, including accidentally
        # pasted credentials. Public errors expose locations, never raw input.
        locations = (", ".join(".".join(map(str, row["loc"])) for row in exc.errors())
                     if isinstance(exc, ValidationError) else "TOML syntax/read")
        raise ModelConfigError(f"Invalid model catalog ({locations}); check field names and types") from None
