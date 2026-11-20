"""Edit the same non-secret model catalog used by the CLI."""

import os
from pathlib import Path
import tomllib
from uuid import uuid4

import tomlkit
from pydantic import ValidationError

from simple_ar.core.artifacts import write_atomically
from simple_ar.core.locking import SessionFileLock
from simple_ar.integrations.model_credentials import write_key
from simple_ar.integrations.model_profiles import ModelCatalog, default_catalog_path


def settings_path() -> Path:
    return Path(os.environ.get("SIMPLE_AR_MODELS_CONFIG") or default_catalog_path()).expanduser().resolve()


def read_settings(path: Path) -> tuple[bytes, ModelCatalog | None]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return b"", None
    try:
        return raw, ModelCatalog.model_validate(tomllib.loads(raw.decode("utf-8")))
    except (ValueError, TypeError):
        raise ValueError("模型目录格式不合法，请先修正 models.toml。") from None


def save_profile(path: Path, original: bytes, *, name: str, endpoint: str, model: str,
                 reference: str = "", key: str = "", default: bool = False) -> None:
    name, endpoint, model, reference = (value.strip() for value in (name, endpoint, model, reference))
    if not name:
        raise ValueError("请输入连接名称。")
    with SessionFileLock(path.with_name(path.name + ".lock")):
        raw, catalog = read_settings(path)
        if raw != original:
            raise ValueError("模型目录已变化，请重新打开设置。")
        document = tomlkit.parse(raw.decode("utf-8")) if catalog else tomlkit.document()
        if not catalog:
            document["version"] = 1
            document["profiles"] = tomlkit.table()
        profiles = document["profiles"]
        if name not in profiles:
            profiles[name] = tomlkit.table()
            profiles[name]["api"] = "openai_chat"
            profiles[name]["capabilities"] = ["text", "code"]
        profile = profiles[name]
        profile["base_url"] = endpoint
        profile["model"] = model
        profile["api_key_env"] = reference or profile.get("api_key_env") or ("SAR_KEY_" + uuid4().hex.upper())
        if default or not catalog:
            if "routes" not in document:
                document["routes"] = tomlkit.table()
            document["routes"]["text"] = name
            if not catalog:
                document["routes"]["default"] = name
        updated = tomlkit.dumps(document)
        try:
            validated = ModelCatalog.model_validate(tomllib.loads(updated)).profiles[name]
        except ValidationError as exc:
            fields = ", ".join(str(row["loc"][-1]) for row in exc.errors())
            raise ValueError(f"连接配置不合法，请检查 {fields}。") from None
        if (default or not catalog) and (validated.api != "openai_chat" or "text" not in validated.capabilities):
            raise ValueError("桌面任务的默认模型需要支持 Chat 文本调用。")
        if key:
            if os.environ.get(validated.api_key_env, "").strip():
                raise ValueError("该变量已由环境提供，请在环境中轮换 Key，或为新连接使用独立凭据名称。")
            write_key(validated.api_key_env, validated.base_url, key)
        write_atomically(path, updated)
