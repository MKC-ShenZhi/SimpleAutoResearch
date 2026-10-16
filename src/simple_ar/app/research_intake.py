from __future__ import annotations

from collections.abc import Iterable, Mapping
import hashlib
import json
from pathlib import Path
import tomllib
from typing import Any

from simple_ar.core.artifacts import write_json, write_text
from simple_ar.research.workflow_contracts import Diagnostic, ResearchAsset, ResearchBrief


class ResearchInputError(ValueError):
    """Raised when a research input cannot be normalized safely."""

    def __init__(self, diagnostics: Iterable[Diagnostic]) -> None:
        self.diagnostics = tuple(diagnostics)
        message = "; ".join(item.message for item in self.diagnostics) or "Invalid research input"
        super().__init__(message)


def parse_research_input(
    request_text: str | Path,
    *,
    config_path: str | Path | None = None,
    asset_paths: Iterable[str | Path] = (),
) -> ResearchBrief:
    """Parse plain text, one Markdown/text file, and optional TOML metadata.

    Parsing is intentionally side-effect-light: it reads only the named input
    and TOML file. It does not call a model, download documents, scan a
    repository, install dependencies, or execute an asset.
    """

    text, source_path, inferred_config = _read_request(request_text)
    selected_config = Path(config_path) if config_path is not None else inferred_config
    config: dict[str, Any] = {}
    if selected_config is not None:
        config = _load_toml(selected_config)
        source_path = source_path or selected_config

    values = _input_values(config)
    if not text.strip():
        text = _text_value(values, "request_text", "topic")
    if not text.strip():
        raise ResearchInputError(
            (Diagnostic("request_text_required", "A research question or request is required."),)
        )

    requests = _collect_asset_requests(values, asset_paths)
    try:
        return ResearchBrief(
            request_text=text,
            brief_id=_text_value(values, "brief_id") or _stable_brief_id(text, requests),
            revision=_positive_int(values.get("revision", values.get("brief_revision")), 1),
            parent_revision=_optional_positive_int(values.get("parent_revision")),
            objective=_text_value(values, "objective"),
            intents=_strings(values.get("intents", values.get("intent"))),
            requested_outputs=_strings(
                values.get("requested_outputs", values.get("outputs"))
            ),
            asset_ids=_strings(values.get("asset_ids")),
            user_hypotheses=_strings(values.get("user_hypotheses", values.get("hypotheses"))),
            hard_constraints=_strings(values.get("hard_constraints", values.get("constraints"))),
            preferences=_strings(values.get("preferences")),
            open_questions=_strings(values.get("open_questions")),
            accepted_assumptions=_strings(values.get("accepted_assumptions")),
            asset_requests=tuple(requests),
            source_path=str(source_path.resolve()) if source_path is not None else None,
        )
    except (TypeError, ValueError) as exc:
        raise ResearchInputError(
            (Diagnostic("brief_invalid", str(exc), field="brief"),)
        ) from exc


def normalize_assets(
    brief: ResearchBrief,
    *,
    input_base_dir: str | Path,
) -> tuple[ResearchAsset, ...]:
    """Resolve named assets and collect only lightweight identity metadata."""

    base_dir = Path(input_base_dir).resolve()
    assets: list[ResearchAsset] = []
    for request in brief.asset_requests:
        locator = str(request["locator"])
        kind = str(request.get("kind") or "file")
        role = str(request.get("role") or "input")
        mutability = str(request.get("mutability") or "read_only")
        requested_id = str(request.get("asset_id") or "")
        allowed_uses = tuple(str(item) for item in request.get("allowed_uses", ()))
        diagnostics: list[Diagnostic] = []

        if _is_remote_locator(locator):
            canonical_locator = locator
            identity = {"locator": locator, "fingerprint_strength": "none"}
            availability = "unknown"
            understanding = "unknown"
            resolved_path: Path | None = None
        else:
            candidate = Path(locator)
            if not candidate.is_absolute():
                candidate = base_dir / candidate
            try:
                resolved_path = candidate.resolve(strict=False)
            except OSError as exc:
                resolved_path = candidate.absolute()
                diagnostics.append(
                    Diagnostic(
                        "asset_path_unresolved",
                        f"Could not fully resolve asset path {locator!r}: {exc}",
                        "warning",
                        "locator",
                    )
                )
            canonical_locator = str(resolved_path)
            if not resolved_path.exists():
                availability = "missing"
                understanding = "unavailable"
                identity = {
                    "locator": canonical_locator,
                    "fingerprint_strength": "none",
                }
                diagnostics.append(
                    Diagnostic(
                        "asset_missing",
                        f"Asset does not exist: {canonical_locator}",
                        "warning",
                        "locator",
                    )
                )
            else:
                availability = "available"
                understanding = "not_inspected"
                identity, stat_diagnostic = _light_identity(resolved_path)
                if stat_diagnostic is not None:
                    diagnostics.append(stat_diagnostic)
                if resolved_path.is_file() and resolved_path.suffix.lower() not in _KNOWN_SUFFIXES:
                    diagnostics.append(
                        Diagnostic(
                            "asset_format_unrecognized",
                            f"No built-in reader is selected for {resolved_path.suffix or 'this'} asset.",
                            "warning",
                            "locator",
                        )
                    )

        if kind == "file":
            kind = _infer_kind(resolved_path, locator)
        asset_id = requested_id or _stable_asset_id(canonical_locator)
        if not allowed_uses:
            allowed_uses = ("read", "reference") if role in {"paper", "dataset", "baseline"} else ("read",)
        assets.append(
            ResearchAsset(
                asset_id=asset_id,
                kind=kind,
                role=role,
                locator=canonical_locator,
                identity=identity,
                availability=availability,
                understanding=understanding,
                mutability=mutability,
                allowed_uses=allowed_uses,
                diagnostics=tuple(diagnostics),
            )
        )
    return tuple(assets)


def validate_brief(
    brief: ResearchBrief,
    assets: Iterable[ResearchAsset],
) -> tuple[Diagnostic, ...]:
    """Validate explicit input facts without judging scientific quality."""

    diagnostics: list[Diagnostic] = []
    if not brief.request_text.strip():
        diagnostics.append(Diagnostic("request_text_required", "A research request is required.", field="request_text"))

    seen: dict[str, str] = {}
    for asset in assets:
        previous = seen.get(asset.asset_id)
        if previous is not None and previous != asset.locator:
            diagnostics.append(
                Diagnostic(
                    "asset_identity_conflict",
                    f"Asset id {asset.asset_id!r} refers to more than one locator.",
                    "error",
                    "asset_ids",
                )
            )
        seen[asset.asset_id] = asset.locator
        diagnostics.extend(asset.diagnostics)

    if brief.asset_ids:
        missing_ids = sorted(set(brief.asset_ids) - set(seen))
        if missing_ids:
            diagnostics.append(
                Diagnostic(
                    "asset_not_normalized",
                    f"Brief references assets that were not normalized: {', '.join(missing_ids)}",
                    "warning",
                    "asset_ids",
                )
            )
    if not brief.requested_outputs:
        diagnostics.append(
            Diagnostic(
                "outputs_unspecified",
                "No requested output was declared; the application must confirm the deliverable.",
                "info",
                "requested_outputs",
            )
        )
    return tuple(diagnostics)


def write_intake_artifacts(
    brief: ResearchBrief,
    assets: Iterable[ResearchAsset],
    output_dir: str | Path,
    *, revision: int | None = None,
) -> dict[str, Path]:
    """Write inspectable brief, asset, and deterministic Markdown summaries."""

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    asset_rows = [asset.to_dict() for asset in assets]
    suffix = f"-r{revision:04d}" if revision is not None else ""
    brief_path = root / f"brief{suffix}.json"
    assets_path = root / f"assets{suffix}.json"
    markdown_path = root / f"brief{suffix}.md"
    write_json(brief_path, brief.to_dict())
    write_json(assets_path, {"schema_version": "research_assets.v1", "assets": asset_rows})
    write_text(markdown_path, _brief_markdown(brief, asset_rows))
    return {"brief": brief_path, "assets": assets_path, "markdown": markdown_path}




def _read_request(value: str | Path) -> tuple[str, Path | None, Path | None]:
    if isinstance(value, Path):
        candidate = value
    else:
        candidate = _existing_file(value)
        if candidate is None:
            return value, None, None
    if candidate.suffix.lower() == ".toml":
        return "", None, candidate
    if candidate.suffix.lower() not in {".md", ".markdown", ".txt"}:
        raise ResearchInputError(
            (
                Diagnostic(
                    "request_file_format",
                    "Request file must be Markdown, TXT, or TOML.",
                    "error",
                    "request_text",
                ),
            )
        )
    try:
        return candidate.read_text(encoding="utf-8"), candidate, None
    except OSError as exc:
        raise ResearchInputError(
            (Diagnostic("request_file_unreadable", str(exc), "error", "request_text"),)
        ) from exc


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ResearchInputError(
            (Diagnostic("config_missing", f"TOML config does not exist: {path}", field="config_path"),)
        )
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ResearchInputError(
            (Diagnostic("config_invalid", f"Could not parse TOML config: {exc}", field="config_path"),)
        ) from exc
    if not isinstance(data, dict):
        raise ResearchInputError(
            (Diagnostic("config_shape", "TOML root must be a table.", field="config_path"),)
        )
    return data


def _input_values(config: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name in ("brief", "research"):
        section = config.get(name)
        if isinstance(section, Mapping):
            values.update(section)
    values.update({key: value for key, value in config.items() if key not in {"brief", "research"}})
    return values


def _collect_asset_requests(
    values: Mapping[str, Any],
    asset_paths: Iterable[str | Path],
) -> list[dict[str, Any]]:
    raw_assets = values.get("assets", values.get("asset_requests", []))
    if raw_assets is None:
        raw_assets = []
    if not isinstance(raw_assets, list):
        raise ResearchInputError(
            (Diagnostic("assets_shape", "assets must be a TOML array of tables.", field="assets"),)
        )
    requests: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_assets):
        if isinstance(raw, str):
            raw = {"locator": raw}
        if not isinstance(raw, Mapping):
            raise ResearchInputError(
                (Diagnostic("asset_invalid", f"Asset {index} must be a table or path.", field="assets"),)
            )
        locator = str(raw.get("locator") or raw.get("path") or "").strip()
        if not locator:
            raise ResearchInputError(
                (Diagnostic("asset_locator_required", f"Asset {index} has no locator or path.", field="assets"),)
            )
        requests.append(
            {
                "asset_id": str(raw.get("asset_id") or _stable_asset_id(locator)),
                "locator": locator,
                "kind": str(raw.get("kind") or _infer_kind(None, locator)),
                "role": str(raw.get("role") or "input"),
                "mutability": str(raw.get("mutability") or "read_only"),
                "allowed_uses": list(_strings(raw.get("allowed_uses"))),
            }
        )

    local_documents = values.get("local_documents", values.get("documents", [])) or []
    if isinstance(local_documents, str):
        local_documents = [local_documents]
    if not isinstance(local_documents, (list, tuple)):
        raise ResearchInputError(
            (Diagnostic("local_documents_shape", "local_documents must be a list of paths.", field="local_documents"),)
        )
    requests.extend(
        {
            "asset_id": _stable_asset_id(str(path)),
            "locator": str(path),
            "kind": _infer_kind(None, str(path)),
            "role": "paper",
            "mutability": "read_only",
            "allowed_uses": ["read", "reference"],
        }
        for path in local_documents
        if str(path).strip()
    )
    requests.extend(
        {
            "asset_id": _stable_asset_id(str(path)),
            "locator": str(path),
            "kind": _infer_kind(None, str(path)),
            "role": "input",
            "mutability": "read_only",
            "allowed_uses": ["read"],
        }
        for path in asset_paths
        if str(path).strip()
    )
    return requests


def _light_identity(path: Path) -> tuple[dict[str, Any], Diagnostic | None]:
    try:
        stat = path.stat()
        identity: dict[str, Any] = {
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "file_type": "directory" if path.is_dir() else "file",
            "fingerprint_strength": "metadata",
        }
        if path.is_file() and stat.st_size <= 1_048_576:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            identity["sha256"] = digest
            identity["fingerprint_strength"] = "content"
        return identity, None
    except OSError as exc:
        return (
            {"fingerprint_strength": "none"},
            Diagnostic("asset_identity_failed", f"Could not inspect asset identity: {exc}", "warning", "locator"),
        )


def _brief_markdown(brief: ResearchBrief, assets: list[dict[str, Any]]) -> str:
    lines = [
        "# Research input",
        "",
        f"- Brief: `{brief.brief_id}` revision {brief.revision}",
        f"- Objective: {brief.objective or '(not explicitly declared)'}",
        f"- Intents: {', '.join(brief.intents) or '(not explicitly declared)'}",
        f"- Requested outputs: {', '.join(brief.requested_outputs) or '(not explicitly declared)'}",
        "",
        "## Request",
        "",
        brief.request_text.rstrip(),
        "",
        "## Assets",
        "",
    ]
    if assets:
        lines.extend(
            f"- `{asset['asset_id']}` ({asset['role']}, {asset['kind']}): {asset['locator']}"
            for asset in assets
        )
    else:
        lines.append("- None declared.")
    if brief.open_questions:
        lines.extend(["", "## Open questions", "", *[f"- {item}" for item in brief.open_questions]])
    return "\n".join(lines) + "\n"


def _existing_file(value: str) -> Path | None:
    try:
        candidate = Path(value)
        return candidate if candidate.is_file() else None
    except (OSError, ValueError):
        return None


def _text_value(values: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = values.get(name)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _positive_int(value: object, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("revision must be a positive integer") from exc
    if parsed < 1:
        raise ValueError("revision must be a positive integer")
    return parsed


def _optional_positive_int(value: object) -> int | None:
    if value is None:
        return None
    return _positive_int(value, 1)


def _stable_brief_id(text: str, requests: list[dict[str, Any]]) -> str:
    payload = json.dumps({"text": text, "assets": requests}, sort_keys=True, default=str)
    return f"brief-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:12]}"


def _stable_asset_id(locator: str) -> str:
    return f"asset-{hashlib.sha256(locator.encode('utf-8')).hexdigest()[:12]}"


def _is_remote_locator(locator: str) -> bool:
    return "://" in locator


def _infer_kind(path: Path | None, locator: str) -> str:
    if path is not None and path.is_dir():
        return "repository" if (path / ".git").exists() else "directory"
    suffix = Path(locator).suffix.lower()
    if suffix in {".md", ".markdown", ".txt"}:
        return "document"
    if suffix == ".pdf":
        return "paper"
    if suffix in {".csv", ".json", ".jsonl", ".parquet", ".npz", ".npy"}:
        return "dataset"
    return "file"


_KNOWN_SUFFIXES = {
    ".md",
    ".markdown",
    ".txt",
    ".pdf",
    ".csv",
    ".json",
    ".jsonl",
    ".parquet",
    ".npz",
    ".npy",
    ".py",
    ".toml",
    ".yaml",
    ".yml",
}


__all__ = [
    "ResearchInputError",
    "normalize_assets",
    "parse_research_input",
    "validate_brief",
    "write_intake_artifacts",
]
