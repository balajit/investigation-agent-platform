# src/investigation_agent_platform/infrastructure/persistence/buffered.py
"""Dev-only buffered persistence: Postgres write-through with SQLite WAL fallback.

Requirement: investigations (and every other persisted aggregate) must land in
the database whenever it is reachable. When it is not, writes are journaled to
a local SQLite write-ahead log (restorable across restarts via a volume-mounted
``IAP_BUFFER_PATH``) and applied to the existing in-memory repositories so
reads keep working. A background flusher replays unflushed entries into
Postgres in WAL order as soon as it is reachable again.

Fail-closed rules (never silently lose or overwrite data):

- Only *connection* errors route to the buffer. Business outcomes
  (``ConcurrencyError``, ``IdempotencyConflictError``, validation, …)
  propagate to the caller immediately and are never buffered.
- Replay is idempotent by construction: investigation saves carry
  ``expected_version`` (OCC); evidence/outbox/idempotency/action writes ride
  on unique constraints (fingerprint, idempotency key, action id);
  timeline/checkpoint/transition replays are verified before marking flushed.
- A replay conflict marks the entry ``HELD`` and logs loudly. Held entries
  are never retried automatically and never overwritten.
- ``dispatch_pending`` (outbox publish+mark) is primary-only: it is never
  buffered and never replayed, so events cannot double-publish.
- Production never uses this module (F-001 fail-fast stays intact); the
  bootstrap wires it only outside production.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

# Repo attribute names on AppContext that get a buffered proxy, with the
# methods considered *writes* (captured to the WAL on primary outage).
# Everything else delegates read-style (primary, else fallback).
MUTATING_METHODS: dict[str, frozenset[str]] = {
    "investigation_repo": frozenset({"create", "save", "delete"}),
    "evidence_repo": frozenset({"save", "save_batch"}),
    "hypothesis_repo": frozenset({"save"}),
    "input_repo": frozenset({"create_requirement", "set_state", "record_fulfillment"}),
    "finding_cluster_repo": frozenset(
        {"create_cluster", "assign_finding", "upsert_finding_embedding"}
    ),
    "timeline_repo": frozenset({"append", "append_batch"}),
    "profile_repo": frozenset({"save"}),
    "checkpoint_repo": frozenset({"save_checkpoint"}),
    "transition_repo": frozenset({"record_transition"}),
    "action_repo": frozenset({"record_action"}),
    "outbox_repo": frozenset({"enqueue"}),
    "idempotency_store": frozenset({"reserve_or_get", "complete"}),
    "artifact_repo": frozenset({"save"}),
    "session_repo": frozenset({"append"}),
    "code_issue_index": frozenset({"record_session"}),
}

# Methods that must run against the primary only: never buffered, never
# replayed. ``dispatch_pending`` publishes to the broker as a side effect —
# buffering it could double-publish after replay.
PRIMARY_ONLY_METHODS: dict[str, frozenset[str]] = {
    "outbox_repo": frozenset({"dispatch_pending"}),
}

_ENTRY_DDL = """
CREATE TABLE IF NOT EXISTS buffered_ops(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_uuid TEXT NOT NULL UNIQUE,
  repo TEXT NOT NULL,
  op TEXT NOT NULL,
  tenant TEXT NOT NULL DEFAULT '',
  entity_id TEXT,
  payload TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'PENDING',
  applied_to_fallback INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_buffered_ops_state ON buffered_ops(state, seq);
"""


def _model_registry() -> dict[str, type]:
    from investigation_agent_platform.domain.evidence.models import Evidence
    from investigation_agent_platform.domain.hypothesis.models import Hypothesis
    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationAction,
    )
    from investigation_agent_platform.domain.knowledge.models import (
        InvestigationSession,
        KnowledgeArtifact,
    )
    from investigation_agent_platform.domain.profile.models import ApplicationProfile
    from investigation_agent_platform.domain.timeline.models import TimelineEvent

    return {
        "Investigation": Investigation,
        "InvestigationAction": InvestigationAction,
        "Evidence": Evidence,
        "Hypothesis": Hypothesis,
        "TimelineEvent": TimelineEvent,
        "ApplicationProfile": ApplicationProfile,
        "KnowledgeArtifact": KnowledgeArtifact,
        "InvestigationSession": InvestigationSession,
    }


def _encode(value: Any) -> Any:
    """Recursively convert call args to JSON-safe structures."""
    from pydantic import BaseModel

    if isinstance(value, BaseModel):
        return {"__model__": type(value).__name__, "data": value.model_dump(mode="json")}
    if isinstance(value, UUID):
        return {"__uuid__": str(value)}
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"__repr__": repr(value)}


def _decode(value: Any, registry: dict[str, type]) -> Any:
    if isinstance(value, dict):
        if "__model__" in value:
            cls = registry.get(value["__model__"])
            if cls is None:
                raise ValueError(f"Unknown buffered model '{value['__model__']}'")
            return cls(**{k: _decode(v, registry) for k, v in value["data"].items()})
        if "__uuid__" in value:
            return UUID(value["__uuid__"])
        if "__datetime__" in value:
            return datetime.fromisoformat(value["__datetime__"])
        if "__repr__" in value:
            raise ValueError(f"Unrecoverable buffered value: {value['__repr__']}")
        return {k: _decode(v, registry) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v, registry) for v in value]
    return value


def is_connection_error(exc: BaseException) -> bool:
    """True only for transport-level DB failures — never business outcomes."""
    from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError, TimeoutError

    if isinstance(exc, (OperationalError, DisconnectionError, InterfaceError, TimeoutError)):
        return True
    return isinstance(exc, (ConnectionError, ConnectionRefusedError, OSError, asyncio.TimeoutError))


def _down_error() -> Exception:
    from sqlalchemy.exc import OperationalError

    return OperationalError(
        "buffered: database unavailable", None, ConnectionRefusedError("primary down")
    )


class DownPrimary:
    """Primary stub used when Postgres is unreachable: every call fails with a
    connection error so the proxy takes the buffer path."""

    def __init__(self, repo_name: str = "unknown") -> None:
        self._repo_name = repo_name

    def __getattr__(self, name: str) -> Any:
        async def _down(*args: Any, **kwargs: Any) -> Any:
            raise _down_error()

        return _down


@dataclass
class WalEntry:
    seq: int
    entry_uuid: str
    repo: str
    op: str
    tenant: str
    entity_id: str | None
    payload: dict[str, Any]
    state: str = "PENDING"
    applied_to_fallback: bool = False
    attempts: int = 0
    last_error: str | None = None


class SqliteWal:
    """Thread-safe SQLite write-ahead log for buffered writes."""

    def __init__(self, path: str) -> None:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_ENTRY_DDL)
            self._conn.commit()

    @property
    def path(self) -> str:
        return self._path

    def append(
        self, repo: str, op: str, tenant: str, entity_id: str | None, payload: dict[str, Any]
    ) -> WalEntry:
        entry_uuid = str(uuid4())
        created = datetime.now(UTC).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO buffered_ops(entry_uuid, repo, op, tenant, entity_id, payload,"
                " state, applied_to_fallback, attempts, created_at)"
                " VALUES(?,?,?,?,?,?, 'PENDING', 0, 0, ?)",
                (entry_uuid, repo, op, tenant, entity_id, json.dumps(payload), created),
            )
            self._conn.commit()
            seq = cur.lastrowid
        assert seq is not None
        return WalEntry(seq, entry_uuid, repo, op, tenant, entity_id, payload)

    def _row_to_entry(self, row: Any) -> WalEntry:
        return WalEntry(
            seq=row[0],
            entry_uuid=row[1],
            repo=row[2],
            op=row[3],
            tenant=row[4],
            entity_id=row[5],
            payload=json.loads(row[6]),
            state=row[7],
            applied_to_fallback=bool(row[8]),
            attempts=row[9],
            last_error=row[10],
        )

    def list_pending(self, limit: int = 500) -> list[WalEntry]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT seq, entry_uuid, repo, op, tenant, entity_id, payload, state,"
                " applied_to_fallback, attempts, last_error FROM buffered_ops"
                " WHERE state='PENDING' ORDER BY seq LIMIT ?",
                (limit,),
            )
            return [self._row_to_entry(r) for r in cur.fetchall()]

    def pending_count(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM buffered_ops WHERE state='PENDING'")
            return int(cur.fetchone()[0])

    def held_count(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM buffered_ops WHERE state='HELD'")
            return int(cur.fetchone()[0])

    def mark_applied(self, seq: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE buffered_ops SET applied_to_fallback=1 WHERE seq=?", (seq,))
            self._conn.commit()

    def mark_flushed(self, seq: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE buffered_ops SET state='FLUSHED', attempts=attempts+1 WHERE seq=?",
                (seq,),
            )
            self._conn.commit()

    def mark_held(self, seq: int, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE buffered_ops SET state='HELD', attempts=attempts+1,"
                " last_error=? WHERE seq=?",
                (error[:2000], seq),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _entity_id(repo: str, op: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
    """Best-effort stable entity key for a captured call (dedup/inspection)."""
    from pydantic import BaseModel

    if repo == "outbox_repo" and op == "enqueue":
        key = kwargs.get("idempotency_key") or (args[3] if len(args) > 3 else None)
        return str(key) if key is not None else None
    if repo == "idempotency_store" and op in ("reserve_or_get", "complete"):
        key = kwargs.get("key") or (args[1] if len(args) > 1 else None)
        return str(key) if key is not None else None
    if repo == "code_issue_index" and op == "record_session":
        fp = kwargs.get("code_issue_fingerprint") or (args[0] if args else None)
        num = kwargs.get("session_number") or (args[1] if len(args) > 1 else None)
        return f"{fp}:{num}" if fp is not None else None
    if repo == "checkpoint_repo" and op == "save_checkpoint":
        inv = kwargs.get("investigation_id") or (args[1] if len(args) > 1 else None)
        step = kwargs.get("step_number") or (args[2] if len(args) > 2 else None)
        return f"{inv}:{step}" if inv is not None else None
    for value in list(args) + list(kwargs.values()):
        if isinstance(value, BaseModel) and hasattr(value, "id"):
            return str(value.id)
        if isinstance(value, UUID):
            return str(value)
    return None


class BufferedProxy:
    """Write-through proxy with WAL-backed fallback for one repository.

    Write methods try the primary first; on connection errors the call is
    journaled and applied to the fallback (in-memory) repo. Reads try the
    primary first and fall back on connection errors. Business exceptions
    always propagate and are never buffered.
    """

    def __init__(self, primary: Any, fallback: Any, wal: SqliteWal, repo_name: str) -> None:
        self._primary = primary
        self._fallback = fallback
        self._wal = wal
        self._repo_name = repo_name
        self._mutating = MUTATING_METHODS.get(repo_name, frozenset())
        self._primary_only = PRIMARY_ONLY_METHODS.get(repo_name, frozenset())
        self._last_connection_error: float | None = None

    @property
    def primary_status(self) -> str:
        """Operator-facing primary reachability: UP unless a connection error
        was seen recently (5-minute decay so flapping is visible)."""
        import time

        if self._last_connection_error is None:
            return "UP"
        if time.monotonic() - self._last_connection_error > 300:
            return "UP"
        return "DOWN"

    def _note_primary_ok(self) -> None:
        self._last_connection_error = None

    def _note_connection_error(self) -> None:
        import time

        self._last_connection_error = time.monotonic()

    @property
    def primary(self) -> Any:
        return self._primary

    @property
    def fallback(self) -> Any:
        return self._fallback

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._primary_only:
            return getattr(self._primary, name)
        target = getattr(self._fallback, name, None)
        if not inspect.iscoroutinefunction(target):
            return getattr(self._primary, name)
        if name in self._mutating:
            return self._write_wrapper(name)
        return self._read_wrapper(name)

    def _read_wrapper(self, name: str) -> Any:
        async def _read(*args: Any, **kwargs: Any) -> Any:
            try:
                result = await getattr(self._primary, name)(*args, **kwargs)
                self._note_primary_ok()
                return result
            except Exception as exc:
                if is_connection_error(exc):
                    self._note_connection_error()
                    return await getattr(self._fallback, name)(*args, **kwargs)
                raise

        return _read

    def _write_wrapper(self, name: str) -> Any:
        async def _write(*args: Any, **kwargs: Any) -> Any:
            try:
                result = await getattr(self._primary, name)(*args, **kwargs)
                self._note_primary_ok()
                return result
            except Exception as exc:
                if not is_connection_error(exc):
                    raise
            self._note_connection_error()
            tenant = str(args[0]) if args else str(kwargs.get("tenant_id", ""))
            entry = self._wal.append(
                repo=self._repo_name,
                op=name,
                tenant=tenant,
                entity_id=_entity_id(self._repo_name, name, args, kwargs),
                payload={
                    "args": _encode(list(args)),
                    "kwargs": _encode(dict(kwargs)),
                },
            )
            logger.warning(
                "Primary store unreachable; buffered write",
                extra={
                    "repo": self._repo_name,
                    "op": name,
                    "tenant": tenant,
                    "wal_seq": entry.seq,
                },
            )
            try:
                result = await getattr(self._fallback, name)(*args, **kwargs)
            except Exception:
                logger.exception(
                    "Fallback apply failed after WAL journal (entry preserved for flush)",
                    extra={"wal_seq": entry.seq},
                )
                raise
            self._wal.mark_applied(entry.seq)
            return result

        return _write

    async def replay_entry(self, entry: WalEntry, registry: dict[str, type]) -> None:
        """Replay one WAL entry against the primary (used by the flusher)."""
        raw_args = entry.payload.get("args", [])
        raw_kwargs = entry.payload.get("kwargs", {})
        args = tuple(_decode(raw_args, registry))
        kwargs = dict(_decode(raw_kwargs, registry))
        if self._repo_name == "transition_repo" and entry.op == "record_transition":
            # Stable id across replays: a crash between commit and FLUSHED-mark
            # re-inserts the same PK instead of duplicating the audit row.
            kwargs["_buffer_id"] = UUID(entry.entry_uuid)
        await getattr(self._primary, entry.op)(*args, **kwargs)


@dataclass
class FlushStats:
    flushed: int = 0
    held: int = 0
    remaining: int = 0
    errors: list[str] = field(default_factory=list)


async def _verify_replay_insert(
    proxy: BufferedProxy, entry: WalEntry, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> bool:
    """After an IntegrityError on replay, check whether the row is already
    there (crash between commit and mark). True = treat as flushed."""
    try:
        primary = proxy.primary
        if entry.repo == "investigation_repo" and entry.op == "create":
            inv_id = kwargs.get("investigation_id") or (args[1] if len(args) > 1 else None)
            tenant = entry.tenant
            found = await primary.get_by_id(tenant, inv_id)
            return found is not None
        if entry.repo == "investigation_repo" and entry.op == "save":
            tenant = entry.tenant
            inv = kwargs.get("investigation") or (args[1] if len(args) > 1 else None)
            expected = kwargs.get("expected_version", 0)
            found = await primary.get_by_id(tenant, inv.id if hasattr(inv, "id") else inv["id"])
            return found is not None and getattr(found, "version", 0) >= expected
        if entry.repo == "timeline_repo" and entry.op in ("append", "append_batch"):
            tenant = entry.tenant
            inv_id = kwargs.get("investigation_id") or (args[2] if len(args) > 2 else None)
            events = kwargs.get("event") or kwargs.get("events") or []
            if not isinstance(events, list):
                events = [events]
            existing = await primary.find_by_investigation_id(tenant, inv_id)
            existing_ids = {getattr(e, "id", None) for e in existing}
            want = {getattr(e, "id", None) for e in events if hasattr(e, "id")}
            return bool(want) and want <= existing_ids
        if entry.repo == "transition_repo":
            return True  # same-PK re-insert means the row is already there
    except Exception as exc:
        logger.warning(
            "Replay verification read failed; holding entry",
            extra={"wal_seq": entry.seq, "error": str(exc)},
        )
    return False


async def flush_once(
    proxies: dict[str, BufferedProxy], wal: SqliteWal, limit: int = 500
) -> FlushStats:
    """Replay pending WAL entries into primaries in seq order (fail-closed)."""
    from sqlalchemy.exc import IntegrityError

    from investigation_agent_platform.domain.common.exceptions import (
        ConcurrencyError,
        DomainException,
        IdempotencyConflictError,
    )

    registry = _model_registry()
    stats = FlushStats()
    for entry in wal.list_pending(limit):
        proxy = proxies.get(entry.repo)
        if proxy is None:
            wal.mark_held(entry.seq, f"No proxy registered for repo '{entry.repo}'")
            stats.held += 1
            continue
        raw_args = entry.payload.get("args", [])
        raw_kwargs = entry.payload.get("kwargs", {})
        try:
            args = tuple(_decode(raw_args, registry))
            kwargs = dict(_decode(raw_kwargs, registry))
        except Exception as exc:
            wal.mark_held(entry.seq, f"Undecodable payload: {exc}")
            stats.held += 1
            logger.error(
                "WAL entry undecodable; held for operator review",
                extra={"wal_seq": entry.seq, "error": str(exc)},
            )
            continue
        # Checkpoint: content-based skip — an identical snapshot already
        # applied means this replay is a duplicate.
        if entry.repo == "checkpoint_repo" and entry.op == "save_checkpoint":
            try:
                tenant = entry.tenant
                inv_id = kwargs.get("investigation_id") or (args[1] if len(args) > 1 else None)
                snapshot = kwargs.get("state_snapshot") or (args[3] if len(args) > 3 else None)
                latest = await proxy.primary.get_latest_checkpoint(tenant, inv_id)
                if latest is not None and latest == snapshot:
                    wal.mark_flushed(entry.seq)
                    stats.flushed += 1
                    continue
            except Exception as exc:
                if is_connection_error(exc):
                    break
                wal.mark_held(entry.seq, f"Checkpoint pre-check failed: {exc}")
                stats.held += 1
                continue
        try:
            if entry.repo == "transition_repo" and entry.op == "record_transition":
                kwargs["_buffer_id"] = UUID(entry.entry_uuid)
            await getattr(proxy.primary, entry.op)(*args, **kwargs)
            wal.mark_flushed(entry.seq)
            stats.flushed += 1
        except Exception as exc:
            if is_connection_error(exc):
                break  # primary still down; retry on the next cycle
            if isinstance(exc, IntegrityError):
                if await _verify_replay_insert(proxy, entry, args, kwargs):
                    wal.mark_flushed(entry.seq)
                    stats.flushed += 1
                else:
                    wal.mark_held(entry.seq, f"IntegrityError, row not verified: {exc}")
                    stats.held += 1
                    logger.error(
                        "WAL replay integrity conflict; held for operator review",
                        extra={"wal_seq": entry.seq, "repo": entry.repo, "op": entry.op},
                    )
                continue
            if isinstance(exc, (ConcurrencyError, IdempotencyConflictError, DomainException)):
                wal.mark_held(entry.seq, f"{type(exc).__name__}: {exc}")
                stats.held += 1
                logger.error(
                    "WAL replay business conflict; held for operator review "
                    "(never auto-overwritten)",
                    extra={
                        "wal_seq": entry.seq,
                        "repo": entry.repo,
                        "op": entry.op,
                        "error": str(exc),
                    },
                )
                continue
            wal.mark_held(entry.seq, f"Unexpected {type(exc).__name__}: {exc}")
            stats.held += 1
            logger.error(
                "WAL replay unexpected failure; held for operator review",
                extra={"wal_seq": entry.seq, "error": str(exc)},
            )
    stats.remaining = wal.pending_count()
    return stats


async def restore_fallbacks(
    proxies: dict[str, BufferedProxy], wal: SqliteWal, limit: int = 2000
) -> int:
    """Re-apply unflushed WAL entries not yet applied to fallbacks (restart
    recovery for the in-memory read side). Returns the restored count."""
    registry = _model_registry()
    restored = 0
    for entry in wal.list_pending(limit):
        if entry.applied_to_fallback:
            continue
        proxy = proxies.get(entry.repo)
        if proxy is None:
            continue
        try:
            args = tuple(_decode(entry.payload.get("args", []), registry))
            kwargs = dict(_decode(entry.payload.get("kwargs", {}), registry))
            if entry.repo == "transition_repo":
                continue  # fallback is a no-op stub; nothing to restore
            await getattr(proxy.fallback, entry.op)(*args, **kwargs)
            wal.mark_applied(entry.seq)
            restored += 1
        except Exception as exc:
            logger.warning(
                "WAL restore apply failed; entry stays pending for flush",
                extra={"wal_seq": entry.seq, "error": str(exc)},
            )
    return restored


async def flusher_loop(
    proxies: dict[str, BufferedProxy],
    wal: SqliteWal,
    interval_seconds: float,
    stop: asyncio.Event,
) -> None:
    """Background task: replay buffered writes whenever the primary is up."""
    while not stop.is_set():
        try:
            stats = await flush_once(proxies, wal)
            if stats.flushed or stats.held:
                logger.info(
                    "Buffer flush cycle",
                    extra={
                        "flushed": stats.flushed,
                        "held": stats.held,
                        "remaining": stats.remaining,
                    },
                )
            held = wal.held_count()
            if held:
                logger.error(
                    "HELD buffer entries need operator review "
                    "(fail-closed replay conflicts are never auto-retried)",
                    extra={"held": held, "wal": wal.path},
                )
        except Exception as exc:
            logger.warning("Buffer flush cycle failed", extra={"error": str(exc)})
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
