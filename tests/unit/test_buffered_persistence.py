"""Buffered persistence: WAL fallback, replay, and fail-closed conflicts."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import OperationalError

from investigation_agent_platform.api.dependencies import (
    InMemoryInvestigationRepository,
    _InMemoryIdempotencyStore,
    _InMemoryOutboxRepository,
)
from investigation_agent_platform.domain.common.exceptions import ConcurrencyError
from investigation_agent_platform.domain.investigation.models import (
    Investigation,
    InvestigationContext,
    InvestigationRequest,
    InvestigationStatus,
)
from investigation_agent_platform.infrastructure.persistence.buffered import (
    BufferedProxy,
    DownPrimary,
    SqliteWal,
    flush_once,
    is_connection_error,
    restore_fallbacks,
)


def _down_exc() -> OperationalError:
    return OperationalError("SELECT 1", None, ConnectionRefusedError("down"))


def _investigation(tenant: str = "tenant-a") -> Investigation:
    now = datetime.now(UTC)
    return Investigation(
        session_id="sess-test",
        application_id="example-app",
        tenant_id=tenant,
        request=InvestigationRequest(
            application_id="example-app",
            problem_description="buffered test",
            session_id="sess-test",
            requested_by="tester",
        ),
        context=InvestigationContext(environment="dev", time_window=(now, now)),
        status=InvestigationStatus.CREATED,
    )


class FakePrimary:
    """Recording primary with switchable availability."""

    def __init__(self) -> None:
        self.up = True
        self.calls: list[tuple[str, tuple, dict]] = []
        self.store: dict = {}

    def _guard(self, name: str, args: tuple, kwargs: dict) -> None:
        self.calls.append((name, args, kwargs))
        if not self.up:
            raise _down_exc()

    async def create(self, tenant_id: str, investigation: Investigation) -> None:
        self._guard("create", (tenant_id, investigation), {})
        self.store[(tenant_id, investigation.id)] = investigation

    async def get_by_id(self, tenant_id: str, investigation_id: UUID):
        if not self.up:
            raise _down_exc()
        return self.store.get((tenant_id, investigation_id))

    async def save(
        self, tenant_id: str, investigation: Investigation, expected_version: int
    ) -> None:
        self._guard("save", (tenant_id, investigation), {"expected_version": expected_version})
        current = self.store.get((tenant_id, investigation.id))
        if current is not None and current.version != expected_version:
            raise ConcurrencyError("version mismatch")
        self.store[(tenant_id, investigation.id)] = investigation

    async def delete(self, tenant_id: str, investigation_id: UUID) -> None:
        self._guard("delete", (tenant_id, investigation_id), {})
        self.store.pop((tenant_id, investigation_id), None)

    async def exists(self, tenant_id: str, investigation_id: UUID) -> bool:
        if not self.up:
            raise _down_exc()
        return (tenant_id, investigation_id) in self.store


def _proxy(tmp_path, primary=None):
    wal = SqliteWal(str(tmp_path / "buf.sqlite3"))
    fallback = InMemoryInvestigationRepository()
    proxy = BufferedProxy(primary or FakePrimary(), fallback, wal, "investigation_repo")
    return proxy, wal


def test_write_through_when_primary_up(tmp_path) -> None:
    import asyncio

    async def _run() -> None:
        proxy, wal = _proxy(tmp_path)
        inv = _investigation()
        await proxy.create("tenant-a", inv)
        assert wal.pending_count() == 0
        assert await proxy.get_by_id("tenant-a", inv.id) is not None

    asyncio.run(_run())


def test_fallback_and_wal_when_primary_down(tmp_path) -> None:
    import asyncio

    async def _run() -> None:
        primary = FakePrimary()
        primary.up = False
        proxy, wal = _proxy(tmp_path, primary)
        inv = _investigation()
        await proxy.create("tenant-a", inv)  # must not raise
        assert wal.pending_count() == 1
        # Read side served from fallback during outage.
        assert await proxy.get_by_id("tenant-a", inv.id) is not None

    asyncio.run(_run())


def test_business_error_never_buffered(tmp_path) -> None:
    import asyncio

    async def _run() -> None:
        class AngryPrimary(FakePrimary):
            async def create(self, tenant_id: str, investigation: Investigation) -> None:
                raise ConcurrencyError("business refusal")

        proxy, wal = _proxy(tmp_path, AngryPrimary())
        with pytest.raises(ConcurrencyError):
            await proxy.create("tenant-a", _investigation())
        assert wal.pending_count() == 0

    asyncio.run(_run())


def test_flush_on_reconnect(tmp_path) -> None:
    import asyncio

    async def _run() -> None:
        primary = FakePrimary()
        primary.up = False
        proxy, wal = _proxy(tmp_path, primary)
        inv = _investigation()
        await proxy.create("tenant-a", inv)
        assert wal.pending_count() == 1
        primary.up = True
        stats = await flush_once({"investigation_repo": proxy}, wal)
        assert stats.flushed == 1 and stats.held == 0
        assert wal.pending_count() == 0
        assert primary.store[("tenant-a", inv.id)].id == inv.id

    asyncio.run(_run())


def test_replay_conflict_held_fail_closed(tmp_path) -> None:
    import asyncio

    async def _run() -> None:
        primary = FakePrimary()
        primary.up = False
        proxy, wal = _proxy(tmp_path, primary)
        inv = _investigation()
        await proxy.create("tenant-a", inv)
        # Someone else writes a newer version directly to the primary.
        primary.up = True
        newer = inv.model_copy(update={"version": 5})
        primary.store[("tenant-a", inv.id)] = newer
        # WAL holds the original create; replay create over existing row:
        # create has no OCC, SQL impl would just insert-or-conflict; here the
        # fake overwrites — emulate conflict via save path instead.
        wal2 = SqliteWal(str(tmp_path / "buf2.sqlite3"))
        proxy2 = BufferedProxy(
            primary, InMemoryInvestigationRepository(), wal2, "investigation_repo"
        )
        stale = inv.model_copy(update={"version": 1})
        # Journal a save with a stale expected version by hand (simulates a
        # buffered save made before the concurrent write landed).
        from investigation_agent_platform.infrastructure.persistence.buffered import _encode

        entry = wal2.append(
            repo="investigation_repo",
            op="save",
            tenant="tenant-a",
            entity_id=str(inv.id),
            payload={
                "args": _encode(["tenant-a", stale]),
                "kwargs": _encode({"expected_version": 1}),
            },
        )
        assert entry.seq > 0
        stats = await flush_once({"investigation_repo": proxy2}, wal2)
        assert stats.held == 1 and stats.flushed == 0
        assert wal2.held_count() == 1
        # Newer row untouched (never overwritten).
        assert primary.store[("tenant-a", inv.id)].version == 5

    asyncio.run(_run())


def test_restore_repopulates_fresh_fallback(tmp_path) -> None:
    import asyncio

    async def _run() -> None:
        primary = FakePrimary()
        primary.up = False
        proxy, wal = _proxy(tmp_path, primary)
        inv = _investigation()
        await proxy.create("tenant-a", inv)
        # Simulate restart: fresh fallback, same WAL file.
        wal2 = SqliteWal(str(tmp_path / "buf.sqlite3"))
        fresh = InMemoryInvestigationRepository()
        proxy2 = BufferedProxy(primary, fresh, wal2, "investigation_repo")
        assert await proxy2.get_by_id("tenant-a", inv.id) is None
        restored = await restore_fallbacks({"investigation_repo": proxy2}, wal2)
        assert restored == 0  # already applied pre-restart; flag set at capture
        # Unapplied entry (crashed between journal and fallback apply).
        from investigation_agent_platform.infrastructure.persistence.buffered import _encode

        inv2 = _investigation()
        wal2.append(
            repo="investigation_repo",
            op="create",
            tenant="tenant-a",
            entity_id=str(inv2.id),
            payload={"args": _encode(["tenant-a", inv2]), "kwargs": _encode({})},
        )
        restored = await restore_fallbacks({"investigation_repo": proxy2}, wal2)
        assert restored == 1
        assert await proxy2.get_by_id("tenant-a", inv2.id) is not None

    asyncio.run(_run())


def test_down_primary_raises_connection_error() -> None:
    import asyncio

    async def _run() -> None:
        stub = DownPrimary("investigation_repo")
        with pytest.raises(Exception) as exc_info:
            await stub.create("t", _investigation())
        assert is_connection_error(exc_info.value)

    asyncio.run(_run())


def test_outbox_dispatch_pending_never_buffered(tmp_path) -> None:
    import asyncio

    async def _run() -> None:
        wal = SqliteWal(str(tmp_path / "buf.sqlite3"))
        fallback = _InMemoryOutboxRepository()
        proxy = BufferedProxy(DownPrimary("outbox_repo"), fallback, wal, "outbox_repo")
        # enqueue IS buffered…
        await proxy.enqueue("tenant-a", uuid4(), "evt", "key-1", {"a": 1})
        assert wal.pending_count() == 1
        # …but dispatch_pending goes primary-only and raises (no silent no-op).
        with pytest.raises(OperationalError):
            await proxy.dispatch_pending("tenant-a", publisher=None)
        assert wal.pending_count() == 1

    asyncio.run(_run())


def test_idempotency_round_trip_through_buffer(tmp_path) -> None:
    import asyncio

    async def _run() -> None:
        from investigation_agent_platform.infrastructure.persistence.buffered import (
            BufferedProxy as BP,
        )

        class FakeIdem:
            def __init__(self) -> None:
                self.up = False
                self.calls: list = []

            async def reserve_or_get(self, tenant_id: str, key: str, request_hash: str):
                self.calls.append(("reserve_or_get", tenant_id, key, request_hash))
                if not self.up:
                    raise _down_exc()
                return None, True

            async def complete(self, tenant_id: str, key: str, response: dict) -> None:
                self.calls.append(("complete", tenant_id, key))
                if not self.up:
                    raise _down_exc()

        wal = SqliteWal(str(tmp_path / "buf.sqlite3"))
        primary = FakeIdem()
        proxy = BP(primary, _InMemoryIdempotencyStore(), wal, "idempotency_store")
        cached, reserved = await proxy.reserve_or_get("tenant-a", "k-1", "hash-1")
        assert reserved is True and cached is None
        await proxy.complete("tenant-a", "k-1", {"id": "x"})
        assert wal.pending_count() == 2
        primary.up = True
        stats = await flush_once({"idempotency_store": proxy}, wal)
        assert stats.flushed == 2
        assert ("complete", "tenant-a", "k-1") in primary.calls

    asyncio.run(_run())


def test_transition_replay_carries_stable_buffer_id(tmp_path) -> None:
    import asyncio

    async def _run() -> None:
        seen: list = []

        class FakeTransitions:
            def __init__(self) -> None:
                self.up = False

            async def record_transition(
                self,
                tenant_id: str,
                investigation_id,
                from_state: str,
                to_state: str,
                reason: str,
                _buffer_id=None,
            ) -> None:
                if not self.up:
                    raise _down_exc()
                seen.append(_buffer_id)

        wal = SqliteWal(str(tmp_path / "buf.sqlite3"))

        class MemTransitions:
            async def record_transition(self, *a, **k) -> None:
                return None

        primary = FakeTransitions()
        proxy = BufferedProxy(primary, MemTransitions(), wal, "transition_repo")
        inv_id = uuid4()
        await proxy.record_transition("tenant-a", inv_id, "A", "B", "why")
        assert wal.pending_count() == 1
        primary.up = True
        stats = await flush_once({"transition_repo": proxy}, wal)
        assert stats.flushed == 1
        assert seen[0] is not None  # stable id passed on replay

    asyncio.run(_run())
