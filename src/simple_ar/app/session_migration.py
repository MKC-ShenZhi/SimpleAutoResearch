"""Small compatibility boundary for creating a canonical successor session.

The importer intentionally does not translate an old session into new stage
state.  It reads a legacy manifest, creates a fresh common-tool
session, and optionally copies explicitly selected small artifacts as evidence
of what was carried forward.  The legacy directory is never modified.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
from typing import Any

from simple_ar.core import ArtifactRef, ArtifactStore, SessionManifest
from simple_ar.research.workflow_contracts import ResearchBrief

from .research_intake import create_research_task


_DEFAULT_OUTPUTS = ("research_summary",)
_DEFAULT_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024


class SessionMigrationError(RuntimeError):
    """Raised when a legacy session cannot be imported safely."""


@dataclass(frozen=True, slots=True)
class LegacyArtifactMapping:
    """One explicitly selected legacy artifact copied to the successor."""

    name: str
    source_ref: ArtifactRef
    destination_ref: ArtifactRef
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source_ref": self.source_ref.to_dict(),
            "destination_ref": self.destination_ref.to_dict(),
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class LegacyArtifactSkip:
    """A requested legacy artifact that was not copied."""

    name: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class SessionMigrationResult:
    """Inspectable result of one legacy-to-canonical session import."""

    source_root: Path
    destination_root: Path
    parent_session: str
    destination_session: str
    migration_ref: ArtifactRef
    imported: tuple[LegacyArtifactMapping, ...] = ()
    skipped: tuple[LegacyArtifactSkip, ...] = ()
    brief_source: str = "manifest_topic"
    budget_status: str = "unknown_not_imported"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "session_migration.v1",
            "source_root": str(self.source_root),
            "destination_root": str(self.destination_root),
            "parent_session": self.parent_session,
            "destination_session": self.destination_session,
            "migration_ref": self.migration_ref.to_dict(),
            "imported": [item.to_dict() for item in self.imported],
            "skipped": [item.to_dict() for item in self.skipped],
            "brief_source": self.brief_source,
            "budget_status": self.budget_status,
            "original_preserved": True,
        }


def import_legacy_session(
    source_root: str | Path,
    destination_root: str | Path,
    *,
    requested_outputs: Iterable[str] | None = None,
    artifact_names: Iterable[str] = (),
    services: Any | None = None,
    max_artifact_bytes: int = _DEFAULT_MAX_ARTIFACT_BYTES,
) -> SessionMigrationResult:
    """Create a new application session from a read-only v1 session.

    Only state references named by ``artifact_names`` are copied.  Copies are
    limited to regular files within the old session and are recorded under
    the common owner's retained evidence inputs. Unknown or unavailable
    state is recorded as skipped rather than represented by a misleading
    canonical reference.
    """

    if max_artifact_bytes < 1:
        raise ValueError("max_artifact_bytes must be positive.")
    source = Path(source_root).expanduser().resolve()
    destination = Path(destination_root).expanduser().resolve()
    _validate_roots(source, destination)

    source_store = ArtifactStore(source)
    try:
        payload = source_store.read_json("session_manifest.json")
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise SessionMigrationError(f"Could not read legacy session manifest: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise SessionMigrationError("Legacy session manifest must be a JSON object.")
    if str(payload.get("schema_version", "session_manifest.v1")) != "session_manifest.v1":
        raise SessionMigrationError("Only session_manifest.v1 can be imported by this boundary.")
    try:
        legacy_manifest = SessionManifest.from_dict(dict(payload))
    except (TypeError, ValueError, KeyError) as exc:
        raise SessionMigrationError(f"Legacy session manifest is invalid: {exc}") from exc

    names = _unique_names(artifact_names)
    plans, skipped = _plan_artifacts(
        source_store,
        legacy_manifest,
        names,
        max_artifact_bytes=max_artifact_bytes,
    )
    brief, brief_source = _successor_brief(
        source_store,
        legacy_manifest,
        requested_outputs=requested_outputs,
    )
    selected_paths = list(dict.fromkeys(path for _, _, path in plans))
    brief = replace(brief, asset_requests=tuple({'locator': str(path), 'role': 'material',
        'kind': 'file', 'mutability': 'read_only', 'allowed_uses': ['read', 'reference']}
        for path in selected_paths))
    application = create_research_task(brief, root=destination, services=services,
        snapshot_sources=True, max_snapshot_bytes=max_artifact_bytes)
    controller = application.session

    imported: list[LegacyArtifactMapping] = []
    for name, source_ref, source_path in plans:
        # The common owner copied and bounded these bytes before returning;
        # this importer records provenance, never a second copy or live mount.
        destination_path = Path(application.state['config']['sources'][f'source{selected_paths.index(source_path)}'])
        relative_path = destination_path.relative_to(controller.store.root)
        destination_ref = controller.store.ref(
            relative_path,
            kind="legacy_import",
            schema=source_ref.schema,
            producer="session_migration",
        )
        imported.append(
            LegacyArtifactMapping(
                name=name,
                source_ref=source_ref,
                destination_ref=destination_ref,
                size_bytes=destination_path.stat().st_size,
                sha256=_sha256(destination_path),
            )
        )

    migration_ref: ArtifactRef
    with controller.mutation_scope():
        migration_ref = controller.store.write_json(
            "compatibility/session_migration.json",
            {
                "schema_version": "session_migration.v1",
                "source_root": str(source),
                "source_manifest_schema": "session_manifest.v1",
                "source_status": legacy_manifest.status,
                "parent_session": legacy_manifest.session_id,
                "destination_session": controller.manifest.session_id,
                "imported": [item.to_dict() for item in imported],
                "skipped": [item.to_dict() for item in skipped],
                "brief_source": brief_source,
                "budget_status": "unknown_not_imported",
                "original_preserved": True,
            },
            kind="session_migration",
            schema="session_migration.v1",
            producer="session_migration",
        )
        controller.manifest.parent_session = legacy_manifest.session_id
        controller.manifest.state_refs["legacy_import"] = migration_ref
        controller.save()

    return SessionMigrationResult(
        source_root=source,
        destination_root=destination,
        parent_session=legacy_manifest.session_id,
        destination_session=controller.manifest.session_id,
        migration_ref=migration_ref,
        imported=tuple(imported),
        skipped=tuple(skipped),
        brief_source=brief_source,
    )


def _validate_roots(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise SessionMigrationError(f"Legacy session root does not exist: {source}")
    if source == destination or source in destination.parents:
        raise SessionMigrationError(
            "Destination must be outside the legacy session root; the source is read-only."
        )
    if destination.exists() and not destination.is_dir():
        raise SessionMigrationError(f"Destination is not a directory: {destination}")
    if destination.is_dir() and any(destination.iterdir()):
        raise FileExistsError(f"Destination session directory is not empty: {destination}")


def _unique_names(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    result: list[str] = []
    for value in values:
        name = str(value).strip()
        if name and name not in result:
            result.append(name)
    return tuple(result)


def _plan_artifacts(
    store: ArtifactStore,
    manifest: SessionManifest,
    names: tuple[str, ...],
    *,
    max_artifact_bytes: int,
) -> tuple[list[tuple[str, ArtifactRef, Path]], list[LegacyArtifactSkip]]:
    plans: list[tuple[str, ArtifactRef, Path]] = []
    skipped: list[LegacyArtifactSkip] = []
    for name in names:
        ref = manifest.state_refs.get(name)
        if ref is None:
            skipped.append(LegacyArtifactSkip(name, "state reference is not present"))
            continue
        if ref.status != "available":
            skipped.append(LegacyArtifactSkip(name, f"artifact status is {ref.status!r}"))
            continue
        path = store.resolve(ref)
        try:
            resolved = path.resolve(strict=True)
        except (FileNotFoundError, OSError):
            skipped.append(LegacyArtifactSkip(name, "referenced artifact is unavailable"))
            continue
        try:
            resolved.relative_to(store.root)
        except ValueError:
            skipped.append(LegacyArtifactSkip(name, "referenced artifact escapes the legacy root"))
            continue
        if not resolved.is_file() or path.is_symlink():
            skipped.append(LegacyArtifactSkip(name, "only regular files can be imported"))
            continue
        size = resolved.stat().st_size
        if size > max_artifact_bytes:
            skipped.append(
                LegacyArtifactSkip(
                    name,
                    f"artifact is {size} bytes; limit is {max_artifact_bytes} bytes",
                )
            )
            continue
        plans.append((name, ref, resolved))
    return plans, skipped


def _successor_brief(
    store: ArtifactStore,
    manifest: SessionManifest,
    *,
    requested_outputs: Iterable[str] | None,
) -> tuple[ResearchBrief, str]:
    outputs = _unique_names(requested_outputs or ())
    old_brief_ref = manifest.state_refs.get("brief")
    if old_brief_ref is not None:
        try:
            payload = store.read_json(old_brief_ref)
            if isinstance(payload, Mapping) and payload.get("schema_version") == "research_brief_input.v1":
                old_brief = ResearchBrief.from_dict(payload)
                return (
                    replace(
                        old_brief,
                        requested_outputs=outputs or old_brief.requested_outputs or _DEFAULT_OUTPUTS,
                        asset_ids=(),
                        asset_requests=(),
                        source_path=None,
                    ),
                    "legacy_brief",
                )
        except (FileNotFoundError, OSError, TypeError, ValueError):
            pass
    topic = manifest.topic.strip()
    return (
        ResearchBrief(
            request_text=topic,
            objective=topic,
            intents=("research",),
            requested_outputs=outputs or _DEFAULT_OUTPUTS,
        ),
        "manifest_topic",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "LegacyArtifactMapping",
    "LegacyArtifactSkip",
    "SessionMigrationError",
    "SessionMigrationResult",
    "import_legacy_session",
]
