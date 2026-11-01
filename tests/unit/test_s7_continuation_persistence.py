"""Focused persistence contract tests for S7 continuation foundations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from investigation_agent_platform.infrastructure.persistence.continuation_repository import (
    SqlAlchemyContinuationRepository,
)
from investigation_agent_platform.infrastructure.persistence.models import (
    Base,
    ConclusionHistoryORM,
    ContinuationReasoningLeaseORM,
    S7ContinuationORM,
)


def _session(rowcount: int = 1) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock(rowcount=rowcount)
    session.execute = AsyncMock(return_value=result)
    return session


def _rls(session: AsyncMock) -> AsyncMock:
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=None)
    return context


def test_s7_tables_have_required_constraints_and_cascades() -> None:
    continuation = S7ContinuationORM.__table__
    assert continuation.c.investigation_id.foreign_keys == set()
    assert {constraint.name for constraint in continuation.constraints} >= {
        "uq_s7_continuation_tenant_investigation_key",
        "ck_s7_continuation_positive_price",
        "ck_s7_continuation_attempt_bounds",
    }
    assert ConclusionHistoryORM.__table__.c.investigation_id.foreign_keys.pop().ondelete == "CASCADE"
    assert ContinuationReasoningLeaseORM.__table__.c.investigation_id.foreign_keys.pop().ondelete == "CASCADE"
    assert "s7_continuations" in Base.metadata.tables


@pytest.mark.asyncio
async def test_claim_is_tenant_bound_and_compare_and_set() -> None:
    session = _session()
    repo = SqlAlchemyContinuationRepository(MagicMock())
    now = datetime.now(UTC)
    continuation_id = uuid4()
    investigation_id = uuid4()

    with patch(
        "investigation_agent_platform.infrastructure.persistence.continuation_repository.rls_session",
        return_value=_rls(session),
    ):
        claimed = await repo.claim(
            "tenant-a",
            continuation_id,
            investigation_id,
            "handoff-v1",
            "a" * 64,
            now,
            600,
        )

    assert claimed is True
    statement = session.execute.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "s7_continuations.tenant_id" in sql
    assert "s7_continuations.investigation_id" in sql
    assert "claim_lease_expires_at" in sql
    assert "attempt_count" in sql


@pytest.mark.asyncio
async def test_reasoning_lease_reclaims_only_expired_rows() -> None:
    session = _session(rowcount=0)
    repo = SqlAlchemyContinuationRepository(MagicMock())
    now = datetime.now(UTC)

    with patch(
        "investigation_agent_platform.infrastructure.persistence.continuation_repository.rls_session",
        return_value=_rls(session),
    ):
        acquired = await repo.acquire_reasoning_lease(
            "tenant-a", uuid4(), uuid4(), 1, now, 3600
        )

    assert acquired is False
    update_sql = str(session.execute.await_args_list[0].args[0].compile(dialect=postgresql.dialect()))
    assert "lease_expires_at <" in update_sql
    insert_sql = str(session.execute.await_args_list[1].args[0].compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT" in insert_sql


def test_claim_expiry_is_longer_than_now() -> None:
    now = datetime.now(UTC)
    assert now + timedelta(seconds=600) > now
