from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from pathlib import Path
import threading
from typing import Any, Literal

from simple_ar.core.artifacts import read_json, write_json


BudgetNumber = int | float
BudgetStatus = Literal["reserved", "settled", "released", "unknown"]


class BudgetError(RuntimeError):
    """Base error for invalid or unavailable budget operations."""


class BudgetExceededError(BudgetError):
    """Raised before an action when a finite limit cannot cover its reserve."""


class BudgetUnknownError(BudgetError):
    """Raised when an unknown prior consumption prevents safe reservation."""


class BudgetConflictError(BudgetError):
    """Raised when a persisted reservation is used with incompatible data."""


@dataclass
class BudgetEntry:
    """One physical or logical resource reservation and its final accounting."""

    reservation_id: str
    reserved: dict[str, BudgetNumber]
    actual: dict[str, BudgetNumber]
    status: BudgetStatus
    session_id: str = ""
    attempt_id: str = ""
    logical_call_id: str = ""
    provider_call_id: str = ""
    purpose: str = ""
    actual_source: str | None = None
    reason: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON representation of this entry."""

        return {
            "reservation_id": self.reservation_id,
            "reserved": dict(self.reserved),
            "actual": dict(self.actual),
            "status": self.status,
            "session_id": self.session_id,
            "attempt_id": self.attempt_id,
            "logical_call_id": self.logical_call_id,
            "provider_call_id": self.provider_call_id,
            "purpose": self.purpose,
            "actual_source": self.actual_source,
            "reason": self.reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BudgetEntry":
        """Load one entry while keeping malformed state visible."""

        if not isinstance(data, Mapping):
            raise BudgetError("Budget entry must be an object")
        reservation_id = data.get("reservation_id")
        if not isinstance(reservation_id, str) or not reservation_id.strip():
            raise BudgetError("Budget entry reservation_id must be a non-empty string")
        status = data.get("status")
        if status not in {"reserved", "settled", "released", "unknown"}:
            raise BudgetError(f"Unknown budget entry status: {status!r}")
        return cls(
            reservation_id=reservation_id,
            reserved=_normalize_amounts(data.get("reserved"), field="reserved"),
            actual=_normalize_amounts(data.get("actual", {}), field="actual"),
            status=status,
            session_id=_optional_text(data.get("session_id")),
            attempt_id=_optional_text(data.get("attempt_id")),
            logical_call_id=_optional_text(data.get("logical_call_id")),
            provider_call_id=_optional_text(data.get("provider_call_id")),
            purpose=_optional_text(data.get("purpose")),
            actual_source=_optional_text_or_none(data.get("actual_source")),
            reason=_optional_text(data.get("reason")),
            created_at=_optional_text(data.get("created_at")),
            updated_at=_optional_text(data.get("updated_at")),
        )


def completed_llm_usage_is_accounted(entries: Iterable[Mapping[str, Any]]) -> bool:
    """Prove completed calls without settling their bounded failed attempts.

    Only a settled response of the same logical call can accompany an unknown
    LLM attempt. Pending reservations, unknown processes and unbounded charges
    never qualify. This pure predicate grants no budget, execution or retry.
    """
    rows = list(entries)
    completed = {row['logical_call_id'] for row in rows if row['status'] == 'settled'
        and row['logical_call_id'] and row['actual'].get('llm_requests', 0) > 0
        and row['actual'].get('total_tokens', 0) > 0}
    return all(row['status'] in {'settled', 'released'} or (
        row['status'] == 'unknown' and row['actual_source'] == 'reservation_bound'
        and row['logical_call_id'] in completed
        and set(row['reserved']) == {'llm_requests', 'total_tokens'}
        and row['reserved']['llm_requests'] == 1
        and type(row['reserved']['total_tokens']) is int and row['reserved']['total_tokens'] > 0
        and row['actual'].get('llm_requests') == 1
        and all(key in row['reserved'] and type(value) in {int, float}
            and math.isfinite(value) and 0 <= value <= row['reserved'][key]
            for key, value in row['actual'].items())) for row in rows)


class BudgetLedger:
    """Small file-backed ledger for bounded, observable resource usage.

    ``limits`` maps resource names such as ``llm_requests`` and
    ``total_tokens`` to a finite non-negative limit.  A missing key or a
    ``None`` value means that resource is tracked but not hard-limited.

    The ledger is deliberately a single-writer component.  Mutations are
    serialized in-process and are written atomically when ``storage_path`` is
    configured.  It does not pretend to be a multi-process database.
    """

    schema_version = "budget_ledger.v1"

    def __init__(
        self,
        limits: Mapping[str, BudgetNumber | None] | None = None,
        *,
        storage_path: Path | None = None,
    ) -> None:
        self.limits = _normalize_limits(limits or {})
        self.storage_path = Path(storage_path) if storage_path is not None else None
        self._entries: dict[str, BudgetEntry] = {}
        self._lock = threading.RLock()
        self._remaining_protections: dict[object, dict[str, BudgetNumber]] = {}

    @contextmanager
    def protect_remaining(self, amounts: Mapping[str, BudgetNumber], *,
                          additional: bool = False) -> Iterator[None]:
        """Temporarily withhold capacity from new reservations on this ledger.

        Active scopes protect the maximum requested floor per dimension.
        An additional scope leaves its own capacity on top of active floors,
        for nested work that must return a result to its caller.
        Floors are in-memory only: they neither grant capacity nor change
        accounting, unknown consumption, or unlimited dimensions. The lock
        guards scope changes, not the caller's entire context body.
        """

        normalized = _normalize_amounts(amounts, field="protection")
        token = object()
        with self._lock:
            if additional:
                normalized = {key: value + self._remaining_floor(key)
                              for key, value in normalized.items()}
            self._remaining_protections[token] = normalized
        try:
            yield
        finally:
            with self._lock:
                del self._remaining_protections[token]

    def _remaining_floor(self, dimension: str) -> BudgetNumber:
        return max(
            (amounts.get(dimension, 0) for amounts in self._remaining_protections.values()),
            default=0,
        )

    @property
    def entries(self) -> tuple[BudgetEntry, ...]:
        """Return entries in reservation creation order."""

        with self._lock:
            return tuple(self._entries.values())

    @classmethod
    def load(cls, path: Path) -> "BudgetLedger":
        """Load a ledger and keep ``path`` as its future persistence target."""

        try:
            data = read_json(Path(path))
        except (OSError, ValueError) as exc:
            raise BudgetError(f"Could not read budget ledger: {path}") from exc
        if not isinstance(data, Mapping):
            raise BudgetError("Budget ledger must be a JSON object")
        if data.get("schema_version") != cls.schema_version:
            raise BudgetError(
                f"Unsupported budget ledger schema: {data.get('schema_version')!r}"
            )
        raw_entries = data.get("entries", [])
        if not isinstance(raw_entries, list):
            raise BudgetError("Budget ledger entries must be a list")

        ledger = cls(data.get("limits", {}), storage_path=Path(path))
        for raw_entry in raw_entries:
            entry = BudgetEntry.from_dict(raw_entry)
            if entry.reservation_id in ledger._entries:
                raise BudgetConflictError(
                    f"Duplicate budget reservation_id: {entry.reservation_id}"
                )
            ledger._entries[entry.reservation_id] = entry
        return ledger

    def reserve(
        self,
        reservation_id: str,
        amounts: Mapping[str, BudgetNumber],
        *,
        session_id: str = "",
        attempt_id: str = "",
        logical_call_id: str = "",
        provider_call_id: str = "",
        purpose: str = "",
    ) -> BudgetEntry:
        """Reserve resources before an external or local action starts.

        Replaying the same reservation with the same amounts is idempotent.
        A released, settled, or unknown reservation must use a new ID for a
        new action; returning the existing entry prevents accidental double
        reservation during recovery.
        """

        reservation_id = _required_text(reservation_id, "reservation_id")
        normalized = _normalize_amounts(amounts, field="amounts")
        with self._lock:
            existing = self._entries.get(reservation_id)
            if existing is not None:
                if existing.reserved != normalized:
                    raise BudgetConflictError(
                        f"Reservation {reservation_id!r} was replayed with different amounts"
                    )
                return existing

            for dimension, amount in normalized.items():
                self._check_available(dimension, amount)

            now = _utc_now()
            entry = BudgetEntry(
                reservation_id=reservation_id,
                reserved=normalized,
                actual={},
                status="reserved",
                session_id=_optional_text(session_id),
                attempt_id=_optional_text(attempt_id),
                logical_call_id=_optional_text(logical_call_id),
                provider_call_id=_optional_text(provider_call_id),
                purpose=_optional_text(purpose),
                created_at=now,
                updated_at=now,
            )
            self._entries[reservation_id] = entry
            self._persist_unlocked()
            return entry

    def settle(
        self,
        reservation_id: str,
        actual: Mapping[str, BudgetNumber],
        *,
        actual_source: str = "provider",
        provider_call_id: str = "",
        reason: str = "",
    ) -> BudgetEntry:
        """Settle one reservation exactly once with measured or estimated use."""

        reservation_id = _required_text(reservation_id, "reservation_id")
        normalized = _normalize_amounts(actual, field="actual")
        actual_source = _required_text(actual_source, "actual_source")
        with self._lock:
            entry = self._require_entry(reservation_id)
            if entry.status == "settled":
                if entry.actual == normalized:
                    return entry
                raise BudgetConflictError(
                    f"Reservation {reservation_id!r} was settled with different actual use"
                )
            if entry.status != "reserved":
                raise BudgetConflictError(
                    f"Cannot settle reservation {reservation_id!r} in {entry.status!r} state"
                )
            missing = sorted(set(entry.reserved) - set(normalized))
            if missing:
                raise BudgetError(
                    f"Settlement for {reservation_id!r} is missing dimensions: {', '.join(missing)}"
                )
            entry.actual = normalized
            entry.actual_source = actual_source
            if provider_call_id:
                entry.provider_call_id = _optional_text(provider_call_id)
            entry.reason = _optional_text(reason)
            entry.status = "settled"
            entry.updated_at = _utc_now()
            self._persist_unlocked()
            return entry

    def release(self, reservation_id: str, *, reason: str = "") -> BudgetEntry:
        """Release a preflight reservation that never reached the boundary."""

        reservation_id = _required_text(reservation_id, "reservation_id")
        with self._lock:
            entry = self._require_entry(reservation_id)
            if entry.status == "released":
                return entry
            if entry.status != "reserved":
                raise BudgetConflictError(
                    f"Cannot release reservation {reservation_id!r} in {entry.status!r} state"
                )
            entry.reason = _optional_text(reason)
            entry.status = "released"
            entry.updated_at = _utc_now()
            self._persist_unlocked()
            return entry

    def mark_unknown(
        self, reservation_id: str, *, reason: str,
        known_actual: Mapping[str, BudgetNumber] | None = None,
        retain_reservation: bool = False,
    ) -> BudgetEntry:
        """Retain a conservative reservation when provider use is uncertain."""

        reservation_id = _required_text(reservation_id, "reservation_id")
        reason = _required_text(reason, "reason")
        with self._lock:
            entry = self._require_entry(reservation_id)
            if entry.status == "unknown":
                return entry
            if entry.status != "reserved":
                raise BudgetConflictError(
                    f"Cannot mark reservation {reservation_id!r} unknown from {entry.status!r} state"
                )
            entry.reason = reason
            entry.actual = _normalize_amounts(known_actual or {}, field="actual")
            entry.actual_source = "reservation_bound" if retain_reservation else None
            entry.status = "unknown"
            entry.updated_at = _utc_now()
            self._persist_unlocked()
            return entry

    def authorize_remaining(
        self,
        dimension: str | Mapping[str, BudgetNumber],
        amount: BudgetNumber | None = None,
        *,
        authorization_id: str,
        reason: str,
    ) -> None:
        """Start an explicitly authorized allowance; preserve all earlier usage.

        The allowance covers subsequent calls, not unknown historical charges.
        A mapping authorizes several dimensions under one id atomically.
        Replaying the same authorization never replenishes spent capacity.
        """
        allowances, authorization_id, reason = _remaining_authorization(
            dimension, amount, authorization_id, reason,
        )
        with self._lock:
            existing = self._check_remaining_authorization(allowances, authorization_id)
            if existing is not None:
                return
            now = _utc_now()
            self._entries[authorization_id] = BudgetEntry(
                authorization_id, allowances, {}, "released",
                actual_source="user_authorization", reason=reason,
                created_at=now, updated_at=now,
            )
            self.limits.update(allowances)
            self._persist_unlocked()

    def validate_remaining(
        self,
        allowances: Mapping[str, BudgetNumber],
        *,
        authorization_id: str,
        reason: str,
    ) -> None:
        """Validate an authorization before its companion session-budget record is saved."""

        normalized, authorization_id, _ = _remaining_authorization(
            allowances, None, authorization_id, reason,
        )
        with self._lock:
            self._check_remaining_authorization(normalized, authorization_id)

    def _check_remaining_authorization(
        self, allowances: dict[str, BudgetNumber], authorization_id: str,
    ) -> BudgetEntry | None:
        unknown = set(allowances) - set(self.limits)
        if unknown:
            raise BudgetError("Cannot authorize unconfigured resource dimensions: " + ", ".join(sorted(unknown)))
        existing = self._entries.get(authorization_id)
        if existing is not None:
            if existing.actual_source != "user_authorization" or existing.reserved != allowances:
                raise BudgetConflictError("Authorization ID already exists with different terms")
            return existing
        if any(
            e.status == "reserved" and set(allowances).intersection(e.reserved)
            for e in self._entries.values()
        ):
            raise BudgetConflictError("Cannot replace an allowance while calls remain in flight")
        return None

    def _allowance_entries(self, dimension: str) -> tuple[BudgetEntry, ...]:
        entries = tuple(self._entries.values())
        start = 0
        for index, entry in enumerate(entries):
            if entry.actual_source == "user_authorization" and dimension in entry.reserved:
                start = index + 1
        return entries[start:]

    def remaining(self, dimension: str) -> BudgetNumber | None:
        """Return spendable capacity after floors, or ``None`` if unlimited/unknown."""

        dimension = _required_text(dimension, "dimension")
        with self._lock:
            limit = self.limits.get(dimension)
            if limit is None:
                return None
            if self._has_unknown(dimension):
                return None
            remaining = (
                limit - self._committed(dimension) - self._reserved(dimension)
                - self._remaining_floor(dimension)
            )
            return max(0, remaining)

    def unknown_dimensions(self, *, unbounded_only: bool = False) -> tuple[str, ...]:
        """Report unknown consumption, optionally excluding retained finite bounds.

        A bounded unknown charge still appears in ordinary accounting and
        withholds its full reservation; it does not make capacity unknowable.
        """

        with self._lock:
            dimensions = {
                dimension
                for entry in self._entries.values()
                if entry.status == "unknown"
                and (not unbounded_only or entry.actual_source != "reservation_bound")
                for dimension in entry.reserved
                if dimension not in entry.actual
            }
            return tuple(sorted(dimensions))

    def over_limit(self) -> dict[str, BudgetNumber]:
        """Return measured/estimated overages without hiding the settled record."""

        with self._lock:
            overages: dict[str, BudgetNumber] = {}
            for dimension, limit in self.limits.items():
                if limit is None:
                    continue
                excess = self._committed(dimension) - limit
                if excess > 0:
                    overages[dimension] = excess
            return overages

    def snapshot(self) -> dict[str, Any]:
        """Return a compact status view for progress displays and diagnostics."""

        with self._lock:
            dimensions = sorted(set(self.limits) | {
                dimension
                for entry in self._entries.values()
                for dimension in (*entry.reserved.keys(), *entry.actual.keys())
            })
            return {
                "schema_version": self.schema_version,
                "limits": dict(self.limits),
                "remaining": {dimension: self.remaining(dimension) for dimension in dimensions},
                "unknown_dimensions": list(self.unknown_dimensions()),
                "over_limit": dict(self.over_limit()),
                "entry_count": len(self._entries),
            }

    def save(self, path: Path | None = None) -> Path:
        """Persist the ledger atomically and return the destination path."""

        with self._lock:
            if path is not None:
                self.storage_path = Path(path)
            if self.storage_path is None:
                raise BudgetError("No storage path configured for budget ledger")
            self._persist_unlocked()
            return self.storage_path

    def to_dict(self) -> dict[str, Any]:
        """Return the complete persisted representation."""

        with self._lock:
            return {
                "schema_version": self.schema_version,
                "limits": dict(self.limits),
                "entries": [entry.to_dict() for entry in self._entries.values()],
            }

    def _check_available(self, dimension: str, amount: BudgetNumber) -> None:
        limit = self.limits.get(dimension)
        if limit is None:
            return
        if self._has_unknown(dimension):
            raise BudgetUnknownError(
                f"Cannot reserve {dimension!r}: prior consumption is unknown"
            )
        if amount == 0:
            return
        available = (
            limit - self._committed(dimension) - self._reserved(dimension)
            - self._remaining_floor(dimension)
        )
        if amount > available:
            raise BudgetExceededError(
                f"Budget exceeded for {dimension!r}: requested {amount}, remaining {max(0, available)}"
            )

    def _require_entry(self, reservation_id: str) -> BudgetEntry:
        entry = self._entries.get(reservation_id)
        if entry is None:
            raise BudgetError(f"Unknown budget reservation: {reservation_id!r}")
        return entry

    def _has_unknown(self, dimension: str) -> bool:
        return any(
            entry.status == "unknown" and dimension in entry.reserved
            and dimension not in entry.actual
            and entry.actual_source != "reservation_bound"
            for entry in self._allowance_entries(dimension)
        )

    def _committed(self, dimension: str) -> BudgetNumber:
        return sum(
            entry.actual.get(dimension, 0)
            for entry in self._allowance_entries(dimension)
            if entry.status in {"settled", "unknown"}
        )

    def _reserved(self, dimension: str) -> BudgetNumber:
        return sum(
            entry.reserved.get(dimension, 0)
            for entry in self._allowance_entries(dimension)
            if entry.status == "reserved" or (
                entry.status == "unknown" and dimension not in entry.actual
                and entry.actual_source == "reservation_bound"
            )
        )

    def _persist_unlocked(self) -> None:
        if self.storage_path is not None:
            write_json(self.storage_path, self.to_dict())


def _normalize_limits(
    limits: Mapping[str, BudgetNumber | None],
) -> dict[str, BudgetNumber | None]:
    if not isinstance(limits, Mapping):
        raise BudgetError("Budget limits must be an object")
    normalized: dict[str, BudgetNumber | None] = {}
    for dimension, limit in limits.items():
        dimension = _required_text(dimension, "limit dimension")
        if limit is not None:
            limit = _number(limit, field=f"limit for {dimension!r}")
        normalized[dimension] = limit
    return normalized


def _remaining_authorization(
    dimension: str | Mapping[str, BudgetNumber],
    amount: BudgetNumber | None,
    authorization_id: str,
    reason: str,
) -> tuple[dict[str, BudgetNumber], str, str]:
    if isinstance(dimension, Mapping):
        if amount is not None:
            raise BudgetError("A multi-dimension allowance must not include a separate amount")
        allowances = _normalize_amounts(dimension, field="allowance")
        if not allowances:
            raise BudgetError("An allowance must include at least one dimension")
    else:
        dimension = _required_text(dimension, "dimension")
        if amount is None:
            raise BudgetError("An allowance amount is required")
        allowances = {dimension: _number(amount, field="allowance")}
    if any(value <= 0 for value in allowances.values()):
        raise BudgetError("Resource allowance amounts must be positive")
    return allowances, _required_text(authorization_id, "authorization_id"), _required_text(reason, "reason")


def _normalize_amounts(value: object, *, field: str) -> dict[str, BudgetNumber]:
    if not isinstance(value, Mapping):
        raise BudgetError(f"Budget {field} must be an object")
    normalized: dict[str, BudgetNumber] = {}
    for dimension, amount in value.items():
        dimension = _required_text(dimension, f"{field} dimension")
        normalized[dimension] = _number(amount, field=f"{field} for {dimension!r}")
    if not normalized and field == "amounts":
        raise BudgetError("Budget reservation amounts cannot be empty")
    return normalized


def _number(value: object, *, field: str) -> BudgetNumber:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BudgetError(f"{field} must be a non-negative number")
    if isinstance(value, float) and not math.isfinite(value):
        raise BudgetError(f"{field} must be finite")
    if value < 0:
        raise BudgetError(f"{field} must be non-negative")
    return value


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BudgetError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _optional_text_or_none(value: object) -> str | None:
    if value is None:
        return None
    return _optional_text(value) or None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
