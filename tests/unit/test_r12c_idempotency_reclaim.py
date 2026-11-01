"""R12C: expired idempotency rows wedge after a rolled-back delete.

An expired row is reclaimed in one transaction; the loser of a reclaim
race reports in-progress. db-suite (disposable Postgres). Written
first, red before the fix.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest


def _store(factory, ttl_seconds=86400):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.infrastructure.persistence.idempotency_repository import (
        SqlAlchemyIdempotencyStore,
    )

    return SqlAlchemyIdempotencyStore(factory, ttl_seconds=ttl_seconds)


async def _insert_row(factory, tenant, key, request_hash, *, status, expires_at):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.infrastructure.persistence.models import (
        IdempotencyKeyORM,
    )

    async with factory() as session:
        session.add(
            IdempotencyKeyORM(
                tenant_id=tenant,
                idempotency_key=key,
                request_hash=request_hash,
                response_status=status,
                response_json={"ok": True} if status != -1 else {},
                expires_at=expires_at,
            )
        )
        await session.commit()


async def _row(factory, tenant, key):  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from investigation_agent_platform.infrastructure.persistence.models import (
        IdempotencyKeyORM,
    )

    async with factory() as session:
        result = await session.scalars(
            select(IdempotencyKeyORM).where(
                IdempotencyKeyORM.tenant_id == tenant,
                IdempotencyKeyORM.idempotency_key == key,
            )
        )
        row = result.first()
        if row is None:
            return None
        return {
            "expires_at": row.expires_at,
            "status": row.response_status,
            "hash": row.request_hash,
        }


@pytest.mark.asyncio
async def test_r12c_t01_expired_row_is_reclaimed(disposable_db) -> None:  # type: ignore[no-untyped-def]
    from r12_disposable_db import dispose_engine, make_session_factory

    from investigation_agent_platform.domain.common.exceptions import (
        IdempotencyInProgressError,
    )
    from investigation_agent_platform.infrastructure.persistence.models import (
        IdempotencyKeyORM,
    )

    factory, engine = make_session_factory(disposable_db)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(IdempotencyKeyORM.__table__.create, checkfirst=True)
        now = datetime.now(UTC)
        await _insert_row(
            factory, "t", "k1", "H1", status=200, expires_at=now - timedelta(hours=1)
        )
        store = _store(factory)
        cached, reserved = await store.reserve_or_get("t", "k1", "H1")
        assert (cached, reserved) == (None, True)
        row = await _row(factory, "t", "k1")
        assert row is not None and row["expires_at"] > now
        # The fresh PENDING lease blocks the next reserve.
        with pytest.raises(IdempotencyInProgressError):
            await store.reserve_or_get("t", "k1", "H1")
    finally:
        await dispose_engine(engine)


@pytest.mark.asyncio
async def test_r12c_t02_expired_row_with_other_payload(disposable_db) -> None:  # type: ignore[no-untyped-def]
    from r12_disposable_db import dispose_engine, make_session_factory

    from investigation_agent_platform.infrastructure.persistence.models import (
        IdempotencyKeyORM,
    )

    factory, engine = make_session_factory(disposable_db)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(IdempotencyKeyORM.__table__.create, checkfirst=True)
        now = datetime.now(UTC)
        await _insert_row(
            factory, "t", "k2", "H-old", status=200, expires_at=now - timedelta(hours=1)
        )
        store = _store(factory)
        cached, reserved = await store.reserve_or_get("t", "k2", "H-new")
        assert (cached, reserved) == (None, True)
        row = await _row(factory, "t", "k2")
        assert row is not None and row["hash"] == "H-new"
    finally:
        await dispose_engine(engine)


@pytest.mark.asyncio
async def test_r12c_t03_race_on_reclaim(disposable_db) -> None:  # type: ignore[no-untyped-def]
    from r12_disposable_db import dispose_engine, make_session_factory

    from investigation_agent_platform.domain.common.exceptions import (
        IdempotencyInProgressError,
    )
    from investigation_agent_platform.infrastructure.persistence.models import (
        IdempotencyKeyORM,
    )

    factory, engine = make_session_factory(disposable_db)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(IdempotencyKeyORM.__table__.create, checkfirst=True)
        now = datetime.now(UTC)
        await _insert_row(
            factory, "t", "k3", "H1", status=200, expires_at=now - timedelta(hours=1)
        )
        store = _store(factory)

        async def _one(i):  # type: ignore[no-untyped-def]
            try:
                return await store.reserve_or_get("t", "k3", "H1")
            except IdempotencyInProgressError:
                return "in-progress"

        results = await asyncio.gather(*[_one(i) for i in range(8)])
        wins = [r for r in results if r == (None, True)]
        assert len(wins) == 1, results
        assert len([r for r in results if r == "in-progress"]) == 7
    finally:
        await dispose_engine(engine)
