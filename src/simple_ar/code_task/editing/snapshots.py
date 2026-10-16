from __future__ import annotations

"""File-level snapshots for bounded code-task edits.

The snapshot layer is intentionally generic: it records only files that an edit
or repair path is about to touch, including files that did not exist before the
attempt. This keeps rollback auditable without copying an entire project tree.
"""

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from simple_ar.code_task.runtime.state import is_relative_to, utcnow_iso
from simple_ar.core.artifacts import write_atomically, write_json


SNAPSHOT_SCHEMA_VERSION = "code_task_file_snapshot.v1"


@dataclass
class FileSnapshotSet:
    """Persist and restore original file bytes for a bounded edit attempt."""

    workspace_dir: Path
    snapshot_dir: Path
    label: str
    _rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    _restore_events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def manifest_path(self) -> Path:
        return self.snapshot_dir / "snapshot.json"

    @property
    def captured_count(self) -> int:
        return len(self._rows)

    @property
    def restored_count(self) -> int:
        return len(self._restore_events)

    def capture(self, relative_path: str) -> dict[str, Any] | None:
        """Capture the current state of ``relative_path`` once.

        The path is workspace-relative. Missing files are recorded as missing so
        rollback can remove files created by a failed attempt.
        """

        rel_path = normalize_snapshot_path(relative_path)
        if not rel_path:
            return None
        if rel_path in self._rows:
            return self._rows[rel_path]
        target = _workspace_file(self.workspace_dir, rel_path)
        if target is None:
            return None

        row: dict[str, Any] = {
            "path": rel_path,
            "captured_at": utcnow_iso(),
            "existed": target.exists(),
            "kind": "missing",
        }
        if target.exists():
            if not target.is_file():
                row["kind"] = "non_file"
                row["restore_supported"] = False
            else:
                data = target.read_bytes()
                snapshot_file = self._snapshot_file(rel_path)
                snapshot_file.parent.mkdir(parents=True, exist_ok=True)
                snapshot_file.write_bytes(data)
                row.update(
                    {
                        "kind": "file",
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "bytes": len(data),
                        "snapshot_file": snapshot_file.relative_to(self.snapshot_dir).as_posix(),
                    }
                )
        self._rows[rel_path] = row
        self.write_manifest()
        return row

    def capture_many(self, relative_paths: Iterable[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in relative_paths:
            row = self.capture(path)
            if row is not None:
                rows.append(row)
        return rows

    def restore(self, relative_paths: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Restore captured files.

        Missing-before files are removed if a failed repair created them. Files
        that existed before the snapshot are restored byte-for-byte.
        """

        selected = (
            [normalize_snapshot_path(path) for path in relative_paths]
            if relative_paths is not None
            else list(self._rows)
        )
        restored: list[dict[str, Any]] = []
        for rel_path in selected:
            if not rel_path or rel_path not in self._rows:
                continue
            row = self._rows[rel_path]
            target = _workspace_file(self.workspace_dir, rel_path)
            if target is None:
                event = {"path": rel_path, "status": "skipped", "reason": "path_escapes_workspace"}
                restored.append(event)
                self._restore_events.append(event)
                continue
            event = {"path": rel_path, "restored_at": utcnow_iso()}
            if not row.get("existed"):
                if target.is_file() or target.is_symlink():
                    try:
                        target.unlink()
                        event["status"] = "removed_created_file"
                    except OSError as exc:
                        event.update(status="failed", reason=str(exc))
                elif target.exists():
                    event["status"] = "skipped"
                    event["reason"] = "created_path_is_not_file"
                else:
                    event["status"] = "already_missing"
                restored.append(event)
                self._restore_events.append(event)
                continue
            if row.get("kind") != "file":
                event["status"] = "skipped"
                event["reason"] = f"unsupported_snapshot_kind:{row.get('kind')}"
                restored.append(event)
                self._restore_events.append(event)
                continue
            snapshot_ref = row.get("snapshot_file")
            snapshot_file = self.snapshot_dir / str(snapshot_ref)
            if not snapshot_file.is_file():
                event["status"] = "failed"
                event["reason"] = "snapshot_file_missing"
                restored.append(event)
                self._restore_events.append(event)
                continue
            if target.exists() and not target.is_file():
                event["status"] = "failed"
                event["reason"] = "target_exists_but_is_not_file"
                restored.append(event)
                self._restore_events.append(event)
                continue
            try:
                write_atomically(target, snapshot_file.read_bytes())
                event["status"] = "restored_file"
            except OSError as exc:
                event.update(status="failed", reason=str(exc))
            restored.append(event)
            self._restore_events.append(event)
        self.write_manifest()
        return restored

    def artifact_record(self) -> dict[str, Any]:
        """Return a compact artifact record for summaries and manifests."""

        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "label": self.label,
            "snapshot_dir": self.snapshot_dir.as_posix(),
            "manifest": self.manifest_path.as_posix(),
            "captured_count": self.captured_count,
            "restored_count": self.restored_count,
        }

    def restore_verified(self) -> list[str]:
        """Restore the captured unit and retain failures without masking its rejection."""

        try:
            events = self.restore()
            errors = [f"Snapshot restore failed for {row['path']}: {row.get('reason', row['status'])}"
                for row in events if row['status'] not in
                {'restored_file', 'removed_created_file', 'already_missing'}]
            for row in self._rows.values():
                path = _workspace_file(self.workspace_dir, row['path'])
                if path is None:
                    errors.append(f"Snapshot restore path escapes workspace: {row['path']}")
                elif row.get('existed'):
                    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row.get('sha256'):
                        errors.append(f"Snapshot restore byte check failed for {row['path']}")
                elif path.exists():
                    errors.append(f"Snapshot restore left created path {row['path']}")
            return errors
        except Exception as exc:
            return [f'Snapshot restoration raised {type(exc).__name__}: {exc}; inspect {self.manifest_path}']

    def write_manifest(self) -> None:
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            self.manifest_path,
            {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "label": self.label,
                "created_or_updated_at": utcnow_iso(),
                "workspace_dir": self.workspace_dir.as_posix(),
                "snapshot_dir": self.snapshot_dir.as_posix(),
                "files": list(self._rows.values()),
                "restore_events": self._restore_events,
            },
        )

    def _snapshot_file(self, relative_path: str) -> Path:
        return self.snapshot_dir / "files" / relative_path


def create_file_snapshot_set(
    *,
    workspace_dir: Path,
    snapshot_root: Path,
    label: str,
) -> FileSnapshotSet:
    """Create a new numbered snapshot set under ``snapshot_root``."""

    snapshot_root.mkdir(parents=True, exist_ok=True)
    slug = _slug(label)
    for index in range(1, 10000):
        candidate = snapshot_root / f"{slug}-{index:03d}"
        if not candidate.exists():
            candidate.mkdir(parents=True)
            snapshot = FileSnapshotSet(
                workspace_dir=Path(workspace_dir).resolve(),
                snapshot_dir=candidate,
                label=slug,
            )
            snapshot.write_manifest()
            return snapshot
    raise RuntimeError(f"Could not allocate snapshot directory under {snapshot_root}")


def normalize_snapshot_path(value: str) -> str:
    text = str(value).replace("\\", "/").strip().lstrip("/")
    if not text:
        return ""
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return ""
    return path.as_posix()


def _workspace_file(workspace_dir: Path, relative_path: str) -> Path | None:
    rel_path = normalize_snapshot_path(relative_path)
    if not rel_path:
        return None
    workspace = Path(workspace_dir).resolve()
    path = (workspace / rel_path).resolve()
    if not is_relative_to(path, workspace):
        return None
    return path


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip().lower()).strip("-._")
    return slug or "snapshot"
