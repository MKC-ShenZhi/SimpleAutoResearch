from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
import re
from contextlib import contextmanager
from functools import wraps
from threading import RLock
from typing import Any, Callable, Iterable, Iterator, Literal, TypeVar

from simple_ar.core.capabilities import (
    ArtifactRef,
    ArtifactStore,
    AttemptManifest,
    CapabilityRegistry,
    CapabilityContext,
    CapabilityResult,
)
from simple_ar.core.profiles import resolve_lifecycle_profile
from simple_ar.core.locking import SessionFileLock


SessionStatus = Literal[
    "created",
    "running",
    "paused",
    "completed",
    "partial",
    "blocked",
    "failed",
]
DecisionAction = Literal["accept", "revise", "repair", "block"]

_SESSION_STATUSES = {
    "created",
    "running",
    "paused",
    "completed",
    "partial",
    "blocked",
    "failed",
}
_DECISION_ACTIONS = {"accept", "revise", "repair", "block"}
_SESSION_MANIFEST_SCHEMAS = {"session_manifest.v1", "session_manifest.v2"}
_SESSION_MANIFEST_SCHEMA = "session_manifest.v2"
_ControllerMethod = TypeVar("_ControllerMethod", bound=Callable[..., Any])


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _persist_attempt_result(
    store: ArtifactStore, result: CapabilityResult, commit_facts: dict[str, Any],
) -> ArtifactRef:
    return store.write_json(
        "capability_result.json", {**result.to_dict(), **commit_facts},
        kind="capability_result", schema="capability_result.v1",
        producer="capability_runtime",
    )


def _locked_method(method: _ControllerMethod) -> _ControllerMethod:
    """Apply the session mutation boundary without changing method semantics."""

    @wraps(method)
    def wrapped(self: "SessionController", *args: Any, **kwargs: Any) -> Any:
        with self._mutation_scope():
            return method(self, *args, **kwargs)

    return wrapped  # type: ignore[return-value]


@dataclass
class BudgetState:
    """Small bounded budget for one research session."""

    max_attempts: int = 3
    max_no_progress: int = 2
    attempts: int = 0
    no_progress: int = 0
    recorded_attempts: list[str] = field(default_factory=list)
    continuation_authorizations: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.max_no_progress < 1:
            raise ValueError("Session budgets must be positive.")
        if self.attempts < 0 or self.no_progress < 0:
            raise ValueError("Session budget counters cannot be negative.")
        if any(not item.strip() for item in self.recorded_attempts):
            raise ValueError("Recorded attempt ids cannot be blank.")
        if len(set(self.recorded_attempts)) != len(self.recorded_attempts):
            raise ValueError("Recorded attempt ids must be unique.")
        if not isinstance(self.continuation_authorizations, dict):
            raise ValueError("Continuation authorizations must be an object.")
        for authorization_id, terms in self.continuation_authorizations.items():
            if not authorization_id.strip() or not isinstance(terms, dict):
                raise ValueError("Continuation authorization entries must be named objects.")
            if type(terms.get("additional_attempts")) is not int or type(terms.get("additional_no_progress")) is not int:
                raise ValueError("Continuation authorization amounts must be integers.")
            if min(terms["additional_attempts"], terms["additional_no_progress"]) < 0:
                raise ValueError("Continuation authorization amounts cannot be negative.")
            if not isinstance(terms.get("reason"), str) or not terms["reason"].strip():
                raise ValueError("Continuation authorization requires a reason.")
            resources = terms.get("resource_allowances", {})
            if not isinstance(resources, dict):
                raise ValueError("Continuation resource authorization terms must be an object.")

    def record(self, progressed: bool, *, attempt_id: str | None = None) -> bool:
        normalized_attempt_id = attempt_id.strip() if attempt_id is not None else None
        if attempt_id is not None and not normalized_attempt_id:
            raise ValueError("Attempt id cannot be blank when recording a budget entry.")
        if normalized_attempt_id and normalized_attempt_id in self.recorded_attempts:
            return False
        self.attempts += 1
        self.no_progress = 0 if progressed else self.no_progress + 1
        if normalized_attempt_id:
            self.recorded_attempts.append(normalized_attempt_id)
        return True

    def exhausted(self) -> bool:
        return self.attempts >= self.max_attempts or self.no_progress >= self.max_no_progress

    def authorize_additional(
        self,
        authorization_id: str,
        *,
        attempts: int = 0,
        no_progress: int = 0,
        resource_allowances: dict[str, int | float] | None = None,
        reason: str,
    ) -> bool:
        """Record explicit caps without executing work or resetting observed use.

        Execution checks capacity separately: a user may replenish resources
        before adding attempts, or replay an authorization after spending it.
        """

        authorization_id = authorization_id.strip()
        reason = reason.strip()
        if not authorization_id or not reason:
            raise ValueError("Continuation authorization requires an id and reason.")
        if type(attempts) is not int or type(no_progress) is not int or min(attempts, no_progress) < 0:
            raise ValueError("Additional attempt allowances must be non-negative integers.")
        resources = dict(resource_allowances or {})
        if attempts == 0 and no_progress == 0 and not resources:
            raise ValueError("At least one additional attempt allowance must be positive.")
        terms = {
            "additional_attempts": attempts,
            "additional_no_progress": no_progress,
            "resource_allowances": resources,
            "reason": reason,
        }
        existing = self.continuation_authorizations.get(authorization_id)
        if existing is not None:
            if existing != terms:
                raise ValueError("Continuation authorization id already exists with different terms.")
            return False
        self.max_attempts += attempts
        self.max_no_progress += no_progress
        self.continuation_authorizations[authorization_id] = terms
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "max_no_progress": self.max_no_progress,
            "attempts": self.attempts,
            "no_progress": self.no_progress,
            "recorded_attempts": list(self.recorded_attempts),
            "continuation_authorizations": {
                key: dict(value) for key, value in self.continuation_authorizations.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BudgetState":
        raw_recorded_attempts = data.get("recorded_attempts", [])
        if not isinstance(raw_recorded_attempts, (list, tuple)):
            raise ValueError("BudgetState.recorded_attempts must be an array.")
        raw_authorizations = data.get("continuation_authorizations", {})
        if not isinstance(raw_authorizations, dict):
            raise ValueError("BudgetState.continuation_authorizations must be an object.")
        if any(not isinstance(key, str) or not isinstance(value, dict) for key, value in raw_authorizations.items()):
            raise ValueError("BudgetState.continuation_authorizations entries must be named objects.")
        return cls(
            max_attempts=int(data.get("max_attempts", 3)),
            max_no_progress=int(data.get("max_no_progress", 2)),
            attempts=int(data.get("attempts", 0)),
            no_progress=int(data.get("no_progress", 0)),
            recorded_attempts=[
                str(item).strip()
                for item in raw_recorded_attempts
                if str(item).strip()
            ],
            continuation_authorizations={
                str(key): dict(value)
                for key, value in raw_authorizations.items()
            },
        )


def _default_session_budget(profile: str | None) -> BudgetState:
    """Give a named profile room for its path plus two explicit recoveries."""

    lifecycle = resolve_lifecycle_profile(profile)
    if lifecycle is None:
        return BudgetState()
    return BudgetState(max_attempts=max(3, len(lifecycle.capabilities) + 2))


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """Auditable decision for one capability attempt."""

    capability: str
    attempt_id: str
    action: DecisionAction
    result_status: str
    reason: str
    progressed: bool
    expected_delta: str = ""
    input_paths: tuple[str, ...] = ()
    output_paths: tuple[str, ...] = ()
    failure_kind: str = "none"
    next_capability: str | None = None
    budget_attempts: int = 0
    budget_no_progress: int = 0
    created_at: str = field(default_factory=_utcnow_iso)

    def __post_init__(self) -> None:
        if not self.capability.strip() or not self.attempt_id.strip():
            raise ValueError("DecisionRecord requires capability and attempt_id.")
        if self.action not in _DECISION_ACTIONS:
            raise ValueError(f"Unsupported decision action: {self.action}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "attempt_id": self.attempt_id,
            "action": self.action,
            "result_status": self.result_status,
            "reason": self.reason,
            "progressed": self.progressed,
            "expected_delta": self.expected_delta,
            "input_paths": list(self.input_paths),
            "output_paths": list(self.output_paths),
            "failure_kind": self.failure_kind,
            "next_capability": self.next_capability,
            "budget_attempts": self.budget_attempts,
            "budget_no_progress": self.budget_no_progress,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DecisionRecord":
        return cls(
            capability=str(data["capability"]),
            attempt_id=str(data["attempt_id"]),
            action=str(data.get("action", "block")),  # type: ignore[arg-type]
            result_status=str(data.get("result_status", "failed")),
            reason=str(data.get("reason", "")),
            progressed=bool(data.get("progressed", False)),
            expected_delta=str(data.get("expected_delta", "")),
            input_paths=tuple(str(item) for item in data.get("input_paths", [])),
            output_paths=tuple(str(item) for item in data.get("output_paths", [])),
            failure_kind=str(data.get("failure_kind", "none")),
            next_capability=(
                str(data["next_capability"])
                if data.get("next_capability")
                else None
            ),
            budget_attempts=int(data.get("budget_attempts", 0)),
            budget_no_progress=int(data.get("budget_no_progress", 0)),
            # Legacy records without a timestamp have unknown creation time;
            # reading them must not manufacture a new time on every load.
            created_at=str(data.get("created_at") or ""),
        )


@dataclass
class SessionManifest:
    """Persisted state for a bounded, capability-oriented research session."""

    session_id: str
    topic: str
    profile: str | None = None
    status: SessionStatus = "created"
    current_attempt: str | None = None
    budget: BudgetState = field(default_factory=BudgetState)
    decisions: list[DecisionRecord] = field(default_factory=list)
    created_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)
    transition_recipe: str = "research-v1"
    status_reason: str = ""
    revision: int = 0
    next_attempt_sequence: int = 1
    state_refs: dict[str, ArtifactRef] = field(default_factory=dict)
    budget_ledger_ref: ArtifactRef | None = None
    lifecycle_owner: str = "session_controller"
    parent_session: str | None = None

    def __post_init__(self) -> None:
        if not self.session_id.strip() or not self.topic.strip():
            raise ValueError("SessionManifest requires session_id and topic.")
        if not self.transition_recipe.strip():
            raise ValueError("SessionManifest.transition_recipe cannot be empty.")
        if self.status not in _SESSION_STATUSES:
            raise ValueError(f"Unsupported session status: {self.status}")
        if self.revision < 0:
            raise ValueError("SessionManifest.revision cannot be negative.")
        if self.next_attempt_sequence < 1:
            raise ValueError("SessionManifest.next_attempt_sequence must be positive.")
        if not self.lifecycle_owner.strip():
            raise ValueError("SessionManifest.lifecycle_owner cannot be empty.")
        if self.parent_session is not None and not self.parent_session.strip():
            raise ValueError("SessionManifest.parent_session cannot be blank.")
        for name, ref in self.state_refs.items():
            if not str(name).strip():
                raise ValueError("SessionManifest state reference names cannot be empty.")
            if not isinstance(ref, ArtifactRef):
                raise TypeError("SessionManifest state references must be ArtifactRef instances.")
        if self.budget_ledger_ref is not None and not isinstance(
            self.budget_ledger_ref, ArtifactRef
        ):
            raise TypeError("SessionManifest.budget_ledger_ref must be an ArtifactRef.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SESSION_MANIFEST_SCHEMA,
            "session_id": self.session_id,
            "topic": self.topic,
            "profile": self.profile,
            "transition_recipe": self.transition_recipe,
            "status": self.status,
            "status_reason": self.status_reason,
            "revision": self.revision,
            "current_attempt": self.current_attempt,
            "next_attempt_sequence": self.next_attempt_sequence,
            "state_refs": {
                name: ref.to_dict() for name, ref in self.state_refs.items()
            },
            "budget_ledger_ref": (
                self.budget_ledger_ref.to_dict()
                if self.budget_ledger_ref is not None
                else None
            ),
            "lifecycle_owner": self.lifecycle_owner,
            "parent_session": self.parent_session,
            "budget": self.budget.to_dict(),
            "decisions": [decision.to_dict() for decision in self.decisions],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionManifest":
        schema_version = str(data.get("schema_version", "session_manifest.v1"))
        if schema_version not in _SESSION_MANIFEST_SCHEMAS:
            raise ValueError(f"Unsupported session manifest schema: {schema_version}")
        raw_state_refs = data.get("state_refs", {})
        if not isinstance(raw_state_refs, dict):
            raise ValueError("SessionManifest.state_refs must be an object.")
        state_refs: dict[str, ArtifactRef] = {}
        for name, ref in raw_state_refs.items():
            if not isinstance(ref, dict):
                raise ValueError(
                    f"SessionManifest state reference {name!r} must be an object."
                )
            state_refs[str(name)] = ArtifactRef.from_dict(ref)
        raw_ledger_ref = data.get("budget_ledger_ref")
        if raw_ledger_ref is not None and not isinstance(raw_ledger_ref, dict):
            raise ValueError("SessionManifest.budget_ledger_ref must be an object or null.")
        return cls(
            session_id=str(data["session_id"]),
            topic=str(data["topic"]),
            profile=str(data["profile"]) if data.get("profile") else None,
            transition_recipe=str(data.get("transition_recipe", "research-v1")),
            status=str(data.get("status", "created")),  # type: ignore[arg-type]
            status_reason=str(data.get("status_reason", "")),
            revision=int(data.get("revision", 0)),
            current_attempt=(
                str(data["current_attempt"]) if data.get("current_attempt") else None
            ),
            next_attempt_sequence=int(data.get("next_attempt_sequence", 1)),
            state_refs=state_refs,
            budget_ledger_ref=(
                ArtifactRef.from_dict(raw_ledger_ref)
                if isinstance(raw_ledger_ref, dict)
                else None
            ),
            lifecycle_owner=str(data.get("lifecycle_owner", "session_controller")),
            parent_session=(
                str(data["parent_session"])
                if data.get("parent_session")
                else None
            ),
            budget=BudgetState.from_dict(data.get("budget", {})),
            decisions=[
                DecisionRecord.from_dict(item)
                for item in data.get("decisions", [])
                if isinstance(item, dict)
            ],
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )


def _reconcile_declared_outputs(
    result: CapabilityResult,
    store: ArtifactStore,
) -> CapabilityResult:
    """Keep available output references honest without scanning an attempt."""

    missing = tuple(
        artifact.path
        for artifact in result.artifacts
        if artifact.status == "available" and not store.exists(artifact)
    )
    if not missing:
        return result
    diagnostics = (
        *result.diagnostics,
        *(f"Declared output artifact is missing: {path}." for path in missing),
    )
    status = "partial" if result.status == "completed" else result.status
    missing_set = set(missing)
    artifacts = tuple(
        replace(artifact, status="missing") if artifact.path in missing_set else artifact
        for artifact in result.artifacts
    )
    return replace(
        result,
        status=status,  # type: ignore[arg-type]
        artifacts=artifacts,
        diagnostics=diagnostics,
    )




class SessionController:
    """Run bounded capability attempts without knowing stage-specific logic."""

    def __init__(
        self,
        store: ArtifactStore,
        registry: CapabilityRegistry,
        manifest: SessionManifest,
        *,
        legacy_read_only: bool = False,
    ):
        self.store = store
        self.registry = registry
        self.manifest = manifest
        self._file_lock = SessionFileLock(self.store.root / ".session.lock")
        self._lock_guard = RLock()
        self._lock_depth = 0
        self._legacy_read_only = legacy_read_only
        self._saved_manifest: dict[str, Any] | None = None

    @contextmanager
    def _mutation_scope(self) -> Iterator[None]:
        """Hold one process and OS lock across a complete mutation."""

        if self._legacy_read_only:
            raise RuntimeError(
                "Legacy session_manifest.v1 is read-only; "
                "use research-session-migrate to import evidence into a new session."
            )
        self._lock_guard.acquire()
        outermost = self._lock_depth == 0
        if outermost:
            try:
                self._file_lock.acquire()
            except Exception:
                self._lock_guard.release()
                raise
        try:
            self._lock_depth += 1
            if outermost and self._saved_manifest is not None:
                if self.store.read_json("session_manifest.json") != self._saved_manifest:
                    raise RuntimeError("Session changed since loading; reload before modifying it.")
            yield
        finally:
            self._lock_depth -= 1
            try:
                if outermost:
                    self._file_lock.release()
            finally:
                self._lock_guard.release()

    @contextmanager
    def mutation_scope(self) -> Iterator[None]:
        """Group an application mutation and its state refs under one lock.

        Capability-level methods already acquire this scope themselves.  A
        higher-level application may use this public wrapper to keep a
        capability result and the corresponding session-state reference from
        being observed separately by another writer.
        """

        with self._mutation_scope():
            yield

    @classmethod
    def create(
        cls,
        root: str | Path,
        *,
        session_id: str,
        topic: str,
        registry: CapabilityRegistry,
        profile: str | None = None,
        budget: BudgetState | None = None,
    ) -> "SessionController":
        store = ArtifactStore(root)
        session_budget = (
            budget if budget is not None else _default_session_budget(profile)
        )
        manifest = SessionManifest(
            session_id=session_id,
            topic=topic,
            profile=profile,
            budget=session_budget,
        )
        controller = cls(store, registry, manifest)
        with controller._mutation_scope():
            if store.exists("session_manifest.json"):
                raise FileExistsError("Session manifest already exists.")
            controller._save_unlocked()
        return controller

    @classmethod
    def load(
        cls,
        root: str | Path,
        *,
        registry: CapabilityRegistry,
    ) -> "SessionController":
        store = ArtifactStore(root)
        payload = store.read_json("session_manifest.json")
        if not isinstance(payload, dict):
            raise ValueError("Session manifest must be a JSON object.")
        manifest = SessionManifest.from_dict(payload)
        schema_version = str(payload.get("schema_version", "session_manifest.v1"))
        controller = cls(
            store,
            registry,
            manifest,
            legacy_read_only=schema_version == "session_manifest.v1",
        )
        controller._saved_manifest = payload
        return controller



    def status_snapshot(self) -> dict[str, Any]:
        """Return a compact, read-only view for status UIs and handoffs.

        The snapshot summarizes persisted attempt manifests and the current
        decision history. It deliberately excludes artifact contents and does
        not select a "best" result, because comparison rules belong to the
        capability domain rather than the session controller.
        """
        attempts = self.list_attempts()
        snapshot: dict[str, Any] = {
            "schema_version": "session_status.v1",
            "session_id": self.manifest.session_id,
            "topic": self.manifest.topic,
            "profile": self.manifest.profile,
            "status": self.manifest.status,
            "status_reason": self.manifest.status_reason,
            "revision": self.manifest.revision,
            "current_attempt": self.manifest.current_attempt,
            "attempt_count": len(attempts),
            "running_attempts": sum(item.status == "running" for item in attempts),
            "active_attempts": [
                {
                    "attempt_id": item.attempt_id,
                    "capability": item.capability,
                    "updated_at": item.updated_at,
                }
                for item in attempts
                if item.status == "running"
            ],
            "completed_attempts": sum(item.status == "completed" for item in attempts),
            "failed_attempts": sum(item.status == "failed" for item in attempts),
            "blocked_attempts": sum(item.status == "blocked" for item in attempts),
            "budget": self.manifest.budget.to_dict(),
            "last_decision": (
                self.manifest.decisions[-1].to_dict()
                if self.manifest.decisions
                else None
            ),
        }
        return snapshot

    def list_attempts(self) -> tuple[AttemptManifest, ...]:
        """Return persisted attempt manifests in creation-independent order."""
        attempts_root = self.store.root / "attempts"
        if not attempts_root.is_dir():
            return ()
        manifests: list[AttemptManifest] = []
        for child in sorted(attempts_root.iterdir(), key=lambda item: item.name):
            manifest_path = child / "attempt_manifest.json"
            if manifest_path.is_file():
                manifests.append(self.store.read_attempt_manifest(manifest_path))
        return tuple(manifests)

    def attempt_lineage(self, attempt_id: str | None = None) -> tuple[AttemptManifest, ...]:
        """Return one persisted attempt lineage from root to the selected node.

        This is an inspection helper only. It does not choose a best result,
        merge artifacts, or schedule another capability.
        """

        selected_id = (attempt_id or self.manifest.current_attempt or "").strip()
        if not selected_id:
            raise ValueError("An attempt id is required when no current attempt exists.")
        attempts = {item.attempt_id: item for item in self.list_attempts()}
        current = attempts.get(selected_id)
        if current is None:
            raise KeyError(f"Unknown attempt: {selected_id}")

        lineage: list[AttemptManifest] = []
        visited: set[str] = set()
        while True:
            if current.attempt_id in visited:
                raise ValueError(
                    f"Attempt lineage contains a cycle at {current.attempt_id}."
                )
            visited.add(current.attempt_id)
            lineage.append(current)
            parent_id = (current.parent_attempt or "").strip()
            if not parent_id:
                return tuple(reversed(lineage))
            parent = attempts.get(parent_id)
            if parent is None:
                raise ValueError(
                    f"Attempt {current.attempt_id} references missing parent {parent_id}."
                )
            current = parent

    def attempt_output_refs(self, attempt_id: str | None = None) -> tuple[ArtifactRef, ...]:
        """Return session-root references for one attempt's declared outputs.

        Artifact paths in an ``AttemptManifest`` are local to that attempt.
        This helper adds the stable ``attempts/<id>/`` prefix so a caller can
        explicitly pass an earlier output to a later capability through the
        session's ``input_store``. It does not select a best result or copy
        any artifact.
        """

        selected_id = (attempt_id or self.manifest.current_attempt or "").strip()
        if not selected_id:
            raise ValueError("An attempt id is required when no current attempt exists.")
        manifest = next(
            (item for item in self.list_attempts() if item.attempt_id == selected_id),
            None,
        )
        if manifest is None:
            raise KeyError(f"Unknown attempt: {selected_id}")
        prefix = Path("attempts") / manifest.attempt_id
        return tuple(
            self.store.ref(
                prefix / artifact.path,
                kind=artifact.kind,
                schema=artifact.schema,
                producer=artifact.producer,
                status=artifact.status,
            )
            for artifact in manifest.outputs
            if artifact.kind != "capability_result"
        )

    def attempt_output_ref(
        self,
        attempt_id: str | None = None,
        *,
        kind: str,
        schema: str | None = None,
    ) -> ArtifactRef:
        """Return one uniquely identified domain output from an attempt."""

        normalized_kind = kind.strip()
        if not normalized_kind:
            raise ValueError("Artifact kind cannot be empty.")
        matches = tuple(
            ref
            for ref in self.attempt_output_refs(attempt_id)
            if ref.kind == normalized_kind
            and (schema is None or ref.schema == schema)
        )
        if not matches:
            selected_id = (attempt_id or self.manifest.current_attempt or "").strip()
            raise KeyError(
                f"Attempt {selected_id!r} has no output with kind {normalized_kind!r}."
            )
        if len(matches) > 1:
            raise ValueError(
                f"Attempt output kind {normalized_kind!r} is ambiguous; "
                "select an artifact by path instead."
            )
        return matches[0]

    def _save_unlocked(self) -> ArtifactRef:
        self.manifest.updated_at = _utcnow_iso()
        payload = self.manifest.to_dict()
        ref = self.store.write_json(
            "session_manifest.json",
            payload,
            kind="session",
            schema=_SESSION_MANIFEST_SCHEMA,
            producer="session_controller",
        )
        self._saved_manifest = payload
        return ref

    @_locked_method
    def save(self) -> ArtifactRef:
        return self._save_unlocked()

    @_locked_method
    def allocate_attempt_id(
        self,
        capability: str,
        *,
        allow_no_progress_exhausted: bool = False,
    ) -> str:
        """Reserve a readable, persistent attempt id for the new API.

        The sequence is advanced before the handler starts.  A skipped number
        after a crash is acceptable; reusing an id could overwrite an attempt
        that already contains execution evidence.
        """

        return self._allocate_attempt_id_unlocked(
            capability,
            allow_no_progress_exhausted=allow_no_progress_exhausted,
        )

    def _allocate_attempt_id_unlocked(
        self,
        capability: str,
        *,
        allow_no_progress_exhausted: bool = False,
    ) -> str:
        """Allocate an id while the caller already owns the mutation lock."""

        normalized = capability.strip()
        if not normalized:
            raise ValueError("Capability name cannot be empty.")
        self._ensure_can_execute(
            allow_no_progress_exhausted=allow_no_progress_exhausted
        )
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", normalized).strip("-._")
        slug = slug or "attempt"
        sequence = max(1, self.manifest.next_attempt_sequence)
        existing = {item.attempt_id for item in self.list_attempts()}
        while f"{slug}-{sequence:04d}" in existing:
            sequence += 1
        attempt_id = f"{slug}-{sequence:04d}"
        self.manifest.next_attempt_sequence = sequence + 1
        self.save()
        return attempt_id

    @_locked_method
    def pause(self, reason: str) -> SessionManifest:
        """Pause the session without inventing a research completion result."""

        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("Pause reason cannot be empty.")
        if self.manifest.status in {"completed", "blocked"}:
            raise RuntimeError(
                f"Session is {self.manifest.status}; start an explicit revision first."
            )
        self._ensure_no_running_attempt()
        self.manifest.status = "paused"
        self.manifest.status_reason = normalized_reason
        self.save()
        return self.manifest

    @_locked_method
    def complete(self, reason: str = "") -> SessionManifest:
        """Mark an application-validated session as complete.

        The core only checks lifecycle legality and durable execution state.
        It deliberately does not decide whether requested research outputs are
        scientifically sufficient; that check belongs to the application layer.
        """

        if self.manifest.status == "completed":
            return self.manifest
        if self.manifest.status == "blocked":
            raise RuntimeError(
                "Blocked session cannot be completed; start an explicit revision first."
            )
        self._ensure_no_running_attempt()
        if not self.list_attempts():
            raise RuntimeError("Session cannot be completed before an attempt exists.")
        self.manifest.status = "completed"
        self.manifest.status_reason = reason.strip() or "Completed by application."
        self.save()
        return self.manifest

    @_locked_method
    def continue_with_revision(
        self,
        reason: str,
        *,
        allow_no_progress_exhausted: bool = False,
    ) -> int:
        """Reopen a settled session with an explicit, budget-preserving revision.

        The recovery escape hatch only permits use of remaining attempts after
        repeated no-progress failures. It does not reset persisted counters or
        bypass the total attempt limit.
        """

        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("Revision reason cannot be empty.")
        if self.manifest.status == "running":
            raise RuntimeError("Session is already running.")
        self._ensure_no_running_attempt()
        if self.manifest.status == "created":
            raise RuntimeError("Session has no settled work to continue.")
        budget = self.manifest.budget
        if budget.attempts >= budget.max_attempts or (
            budget.no_progress >= budget.max_no_progress
            and not allow_no_progress_exhausted
        ):
            raise RuntimeError(
                "Session budget is exhausted; continuation cannot reset the budget."
            )
        self.manifest.revision += 1
        self.manifest.status = "running"
        self.manifest.status_reason = normalized_reason
        self.save()
        return self.manifest.revision

    @_locked_method
    def execute_attempt(
        self,
        capability: str,
        *,
        attempt_id: str | None = None,
        trigger: str = "initial",
        profile: str | None = None,
        inputs: Iterable[ArtifactRef] = (),
        parent_attempt_id: str | None = None,
        progressed: bool | None = None,
        allow_no_progress_exhausted: bool = False,
        **kwargs: Any,
    ) -> CapabilityResult:
        """Execute one capability without choosing its research next step.

        The application owns research decisions. This boundary owns validation,
        attempt persistence, result reconciliation, and execution accounting.
        """

        selected_attempt_id = (
            attempt_id.strip() if attempt_id is not None and attempt_id.strip() else None
        )
        if attempt_id is not None and selected_attempt_id is None:
            raise ValueError("Attempt id cannot be empty.")
        capability = capability.strip()
        if not capability:
            raise ValueError("Capability name cannot be empty.")
        attempt_id = selected_attempt_id or self._allocate_attempt_id_unlocked(
            capability,
            allow_no_progress_exhausted=allow_no_progress_exhausted,
        )
        self._ensure_can_execute(
            allow_no_progress_exhausted=allow_no_progress_exhausted
        )
        input_refs = tuple(inputs)
        attempt_profile = self._resolve_attempt_profile(profile)
        self._ensure_profile_capability(capability)
        parent_id = self._resolve_parent_attempt_id(parent_attempt_id)
        self.registry.resolve(capability)
        self._validate_input_refs(input_refs)
        if "context" in kwargs:
            raise ValueError("Capability context is managed by SessionController.")

        attempt_store, attempt = self.store.new_attempt(
            attempt_id,
            parent_attempt=parent_id,
            trigger=trigger,
            profile=attempt_profile,
            capability=capability,
            inputs=input_refs,
        )
        self.manifest.status = "running"
        self.manifest.current_attempt = attempt_id
        self.manifest.status_reason = ""
        attempt = replace(attempt, status="running", updated_at=_utcnow_iso())
        attempt_store.write_attempt_manifest(attempt)
        self.save()

        try:
            result = self.registry.run(
                capability,
                context=CapabilityContext(
                    store=attempt_store,
                    attempt=attempt,
                    inputs=input_refs,
                    profile=attempt_profile,
                    input_store=self.store,
                ),
                **kwargs,
            )
        except Exception as exc:
            result = CapabilityResult(
                status="failed",
                diagnostics=(f"{type(exc).__name__}: {exc}",),
                provenance={"capability": capability},
            )

        # Save the accounting override atomically with the outcome, so recovery
        # after either manifest write uses the caller's original progress fact.
        commit_facts: dict[str, Any] = {}
        if progressed is not None:
            commit_facts["session_progressed"] = progressed
        if result.provenance.get("recovery") == "explicit_interruption":
            commit_facts["session_interruption_reason"] = None
        _persist_attempt_result(attempt_store, result, commit_facts)
        return self._commit_attempt_result(attempt_store, attempt)

    @_locked_method
    def reconcile_attempt(self, attempt_id: str | None = None) -> CapabilityResult:
        """Reconcile a persisted result, including an unindexed terminal attempt.

        This is intentionally separate from ``recover_interrupted``.  The
        latter records a confirmed missing-result interruption as failed; this
        method trusts only the persisted capability result and never runs a
        handler again.
        """

        selected_id = (attempt_id or self.manifest.current_attempt or "").strip()
        if not selected_id:
            raise ValueError("An attempt id is required when no current attempt exists.")
        attempt = next(
            (item for item in self.list_attempts() if item.attempt_id == selected_id),
            None,
        )
        if attempt is None:
            raise KeyError(f"Unknown attempt: {selected_id}")
        if attempt.status not in {"running", "completed", "failed", "blocked"}:
            raise ValueError(
                f"Attempt {selected_id} is {attempt.status}, not running."
            )
        attempt_store = ArtifactStore(self.store.root / "attempts" / selected_id)
        if not attempt_store.exists("capability_result.json"):
            raise RuntimeError(
                f"Attempt {selected_id} has no persisted result; "
                "use recover_interrupted() after confirming the interruption."
            )

        return self._commit_attempt_result(attempt_store, attempt, reconciled=True)

    @_locked_method
    def recover_interrupted(
        self,
        attempt_id: str | None = None,
        *,
        reason: str = "Process interrupted before the capability result was persisted.",
    ) -> CapabilityResult:
        """Close one manually confirmed interrupted attempt as a failure.

        A process-level interruption can happen before ``execute_attempt`` reaches its
        normal result persistence path.  This explicit operation makes that
        state visible without retrying or selecting a replacement capability;
        callers remain responsible for deciding whether to create a new
        attempt.  Attempts with an existing result envelope are left alone so
        recovery cannot overwrite a potentially usable outcome.
        """

        if self.manifest.status in {"completed", "blocked"}:
            raise RuntimeError(
                f"Session is {self.manifest.status}; interrupted recovery is not allowed."
            )
        selected_id = (attempt_id or self.manifest.current_attempt or "").strip()
        if not selected_id:
            raise ValueError("An attempt id is required when no current attempt exists.")
        if not reason.strip():
            raise ValueError("Recovery reason cannot be empty.")
        attempt = next(
            (item for item in self.list_attempts() if item.attempt_id == selected_id),
            None,
        )
        if attempt is None:
            raise KeyError(f"Unknown attempt: {selected_id}")
        if attempt.status != "running":
            raise ValueError(
                f"Attempt {selected_id} is {attempt.status}, not running."
            )
        attempt_store = ArtifactStore(self.store.root / "attempts" / attempt.attempt_id)
        if attempt_store.exists("capability_result.json"):
            raise RuntimeError(
                f"Attempt {selected_id} already has capability_result.json; inspect it before recovery."
            )
        capability = (attempt.capability or "").strip()
        if not capability:
            raise ValueError(f"Attempt {selected_id} has no capability name.")

        result = CapabilityResult(
            status="failed",
            diagnostics=(reason.strip(),),
            provenance={
                "capability": capability,
                "recovery": "explicit_interruption",
            },
        )
        _persist_attempt_result(
            attempt_store, result, {"session_interruption_reason": reason.strip()},
        )
        return self._commit_attempt_result(attempt_store, attempt)

    def _commit_attempt_result(
        self,
        attempt_store: ArtifactStore,
        attempt: AttemptManifest,
        *,
        reconciled: bool = False,
    ) -> CapabilityResult:
        """Commit only saved outcome facts under the caller's mutation lock.

        The result precedes the attempt manifest, which precedes session
        accounting. Either save gap can be reconciled without executing work.
        Older envelopes lack a progress override and retain status-based
        accounting. Initial output validation is saved in the outcome;
        later invalidation affects only the committed view.
        """

        payload = attempt_store.read_json("capability_result.json")
        saved_result = CapabilityResult.from_dict(payload)
        result = _reconcile_declared_outputs(saved_result, attempt_store)
        progressed = payload.get("session_progressed")
        if progressed is not None and type(progressed) is not bool:
            raise ValueError("Saved session_progressed must be a boolean.")
        if not reconciled and result != saved_result:
            _persist_attempt_result(
                attempt_store, result,
                {key: payload[key] for key in ("session_progressed", "session_interruption_reason") if key in payload},
            )
        result_ref = attempt_store.ref(
            "capability_result.json",
            kind="capability_result",
            schema="capability_result.v1",
            producer="capability_runtime",
        )
        if all(artifact.path != result_ref.path for artifact in result.artifacts):
            result = replace(result, artifacts=(*result.artifacts, result_ref))
        recorded = self.manifest.budget.record(
            progressed if progressed is not None else result.status in {"completed", "partial"},
            attempt_id=attempt.attempt_id,
        )
        attempt_status = result.status if result.status in {"failed", "blocked"} else "completed"
        attempt_store.write_attempt_manifest(
            replace(
                attempt,
                status=attempt_status,  # type: ignore[arg-type]
                outputs=result.artifacts,
                updated_at=_utcnow_iso(),
            )
        )
        interruption_reason = payload.get("session_interruption_reason")
        # Old interrupted results used provenance alone. Only recovery consumes
        # that legacy marker; a new handler's provenance cannot settle a session.
        if (
            "session_interruption_reason" not in payload and reconciled
            and result.status == "failed"
            and result.provenance.get("recovery") == "explicit_interruption"
            and result.diagnostics
        ):
            interruption_reason = result.diagnostics[0]
        if interruption_reason is not None:
            if recorded or attempt.status == "running":
                exhausted = self.manifest.budget.exhausted()
                self.manifest.status = "blocked" if exhausted else "running"
                self.manifest.status_reason = (
                    "Session budget exhausted." if exhausted else interruption_reason
                )
        elif reconciled and attempt.status == "running":
            self.manifest.current_attempt = attempt.attempt_id
            self.manifest.status = "running"
            self.manifest.status_reason = "Reconciled persisted attempt result."
        self.save()
        return result

    def _ensure_can_execute(self, *, allow_no_progress_exhausted: bool = False) -> None:
        if self.manifest.status in {"completed", "blocked", "paused"}:
            if self.manifest.status == "paused":
                raise RuntimeError(
                    "Session is paused; call continue_with_revision() before creating another attempt."
                )
            raise RuntimeError(f"Session is {self.manifest.status}; no further attempt is allowed.")
        self._ensure_no_running_attempt()
        budget = self.manifest.budget
        if budget.attempts >= budget.max_attempts or (
            budget.no_progress >= budget.max_no_progress
            and not allow_no_progress_exhausted
        ):
            self.manifest.status = "blocked"
            self.manifest.status_reason = "Session budget exhausted."
            self.save()
            raise RuntimeError("Session budget is exhausted.")

    def _ensure_no_running_attempt(self) -> None:
        """Reject lifecycle changes while an attempt still needs recovery."""

        running_attempts = tuple(
            item.attempt_id for item in self.list_attempts() if item.status == "running"
        )
        if running_attempts:
            raise RuntimeError(
                "Session has an interrupted running attempt: "
                + ", ".join(running_attempts)
                + "; call recover_interrupted() before creating another attempt."
            )

    def _validate_input_refs(self, inputs: tuple[ArtifactRef, ...]) -> None:
        """Reject missing handoffs before creating an attempt or calling a handler."""

        for ref in inputs:
            if not isinstance(ref, ArtifactRef):
                raise TypeError("Session inputs must be ArtifactRef instances.")
            if ref.status in {"missing", "not_rendered"}:
                raise ValueError(
                    f"Input artifact {ref.path!r} is marked {ref.status}; "
                    "provide an available artifact instead."
                )
            try:
                self.store.require(ref)
            except FileNotFoundError as exc:
                raise FileNotFoundError(
                    f"Input artifact does not exist in the session store: {ref.path}"
                ) from exc

    def _ensure_profile_capability(self, capability: str) -> None:
        lifecycle = resolve_lifecycle_profile(self.manifest.profile)
        if lifecycle is not None and not lifecycle.allows(capability):
            raise ValueError(
                f"Capability {capability.strip()} is outside lifecycle profile "
                f"{lifecycle.name}."
            )

    def _resolve_attempt_profile(self, profile: str | None) -> str | None:
        """Keep attempt metadata inside the session's optional profile scope."""

        session_profile = self.manifest.profile
        requested_profile = profile.strip() if profile and profile.strip() else None
        if session_profile and requested_profile:
            if session_profile.strip() != requested_profile:
                raise ValueError(
                    f"Attempt profile {requested_profile} cannot override session "
                    f"profile {session_profile.strip()}."
                )
            return session_profile
        return session_profile or requested_profile


    def _resolve_parent_attempt_id(self, parent_attempt_id: str | None) -> str | None:
        """Resolve the linear parent or an explicitly requested branch parent."""

        if parent_attempt_id is None:
            return self.manifest.current_attempt
        normalized = parent_attempt_id.strip()
        if not normalized:
            raise ValueError("parent_attempt_id cannot be blank.")
        parent = next(
            (item for item in self.list_attempts() if item.attempt_id == normalized),
            None,
        )
        if parent is None:
            raise KeyError(f"Unknown parent attempt: {normalized}")
        if parent.status not in {"completed", "failed"}:
            raise ValueError(
                f"Parent attempt {normalized} is {parent.status}; "
                "only completed or failed attempts can be branched from."
            )
        return parent.attempt_id
