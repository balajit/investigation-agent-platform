"""WP-C2 hypothesis CAS: insert-at-0, guarded N→N+1, conflicts fail closed.

Covers the InMemory contract plus the SQLAlchemy statement shape
(single conditional UPDATE; rowcount decides; insert race maps
IntegrityError to ConcurrencyError). Mirrors the investigation-CAS
mocked-session pattern — no live Postgres in unit tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from investigation_agent_platform.api.dependencies import InMemoryHypothesisRepository
from investigation_agent_platform.domain.common.exceptions import ConcurrencyError
from investigation_agent_platform.domain.hypothesis.models import Hypothesis

APP = "investigation_agent_platform.infrastructure.persistence"


def _hyp(**over: object) -> Hypothesis:
    base: dict[str, object] = {
        "tenant_id": "tenant-a",
        "investigation_id": uuid4(),
        "title": "t",
        "description": "d",
        "statement": "s",
    }
    base.update(over)
    return Hypothesis(**base)  # type: ignore[arg-type]


def _inv_id(h: Hypothesis):
    return h.investigation_id


class TestInMemoryCas:
    @pytest.mark.asyncio
    async def test_insert_at_zero_starts_at_one(self) -> None:
        repo = InMemoryHypothesisRepository()
        h = _hyp()
        await repo.save("tenant-a", h, _inv_id(h), 0)
        stored = await repo.get_by_id("tenant-a", h.id)
        assert stored is not None and stored.version == 1

    @pytest.mark.asyncio
    async def test_double_insert_conflicts(self) -> None:
        repo = InMemoryHypothesisRepository()
        h = _hyp()
        await repo.save("tenant-a", h, _inv_id(h), 0)
        with pytest.raises(ConcurrencyError):
            await repo.save("tenant-a", h, _inv_id(h), 0)

    @pytest.mark.asyncio
    async def test_guarded_update_bumps(self) -> None:
        repo = InMemoryHypothesisRepository()
        h = _hyp()
        await repo.save("tenant-a", h, _inv_id(h), 0)
        nxt = h.model_copy(update={"statement": "s2"})
        await repo.save("tenant-a", nxt, _inv_id(h), 1)
        stored = await repo.get_by_id("tenant-a", h.id)
        assert stored is not None
        assert stored.version == 2 and stored.statement == "s2"

    @pytest.mark.asyncio
    async def test_stale_version_conflicts(self) -> None:
        repo = InMemoryHypothesisRepository()
        h = _hyp()
        await repo.save("tenant-a", h, _inv_id(h), 0)
        await repo.save("tenant-a", h.model_copy(update={"statement": "s2"}), _inv_id(h), 1)
        with pytest.raises(ConcurrencyError):
            await repo.save("tenant-a", h.model_copy(update={"statement": "s3"}), _inv_id(h), 1)

    @pytest.mark.asyncio
    async def test_update_missing_conflicts(self) -> None:
        repo = InMemoryHypothesisRepository()
        with pytest.raises(ConcurrencyError):
            await repo.save("tenant-a", _hyp(), uuid4(), 1)

    @pytest.mark.asyncio
    async def test_tenant_mismatch_conflicts(self) -> None:
        repo = InMemoryHypothesisRepository()
        h = _hyp()
        with pytest.raises(ConcurrencyError):
            await repo.save("tenant-b", h, _inv_id(h), 0)

    @pytest.mark.asyncio
    async def test_interleaving_second_writer_loses(self) -> None:
        """Controlled interleave: A and B read v1; A commits; B is stale."""
        repo = InMemoryHypothesisRepository()
        h = _hyp()
        await repo.save("tenant-a", h, _inv_id(h), 0)
        a = (await repo.get_by_id("tenant-a", h.id)).model_copy(update={"statement": "a"})  # type: ignore[union-attr]
        b = (await repo.get_by_id("tenant-a", h.id)).model_copy(update={"statement": "b"})  # type: ignore[union-attr]
        await repo.save("tenant-a", a, _inv_id(h), 1)
        with pytest.raises(ConcurrencyError):
            await repo.save("tenant-a", b, _inv_id(h), 1)
        stored = await repo.get_by_id("tenant-a", h.id)
        assert stored is not None and stored.statement == "a" and stored.version == 2


def _session_with(execute_result: MagicMock | None = None) -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=execute_result)
    session.flush = AsyncMock()
    session.rollback = AsyncMock()
    session.add = MagicMock()
    return session


def _cm(session: AsyncMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


class TestSqlAlchemyCas:
    @pytest.mark.asyncio
    async def test_update_single_conditional_statement(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.hypothesis_repository import (
            SqlAlchemyHypothesisRepository,
        )

        result = MagicMock()
        result.rowcount = 1
        session = _session_with(result)
        h = _hyp()
        with patch(f"{APP}.hypothesis_repository.rls_session", return_value=_cm(session)):
            repo = SqlAlchemyHypothesisRepository(db_session_factory=MagicMock())
            await repo.save("tenant-a", h, _inv_id(h), 1)
        assert session.execute.await_count == 1
        sent = session.execute.await_args.args[0]
        assert "hypotheses" in str(sent) and "version" in str(sent)

    @pytest.mark.asyncio
    async def test_update_rowcount_zero_conflicts(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.hypothesis_repository import (
            SqlAlchemyHypothesisRepository,
        )

        result = MagicMock()
        result.rowcount = 0
        session = _session_with(result)
        with patch(f"{APP}.hypothesis_repository.rls_session", return_value=_cm(session)):
            repo = SqlAlchemyHypothesisRepository(db_session_factory=MagicMock())
            with pytest.raises(ConcurrencyError):
                await repo.save("tenant-a", _hyp(), uuid4(), 1)

    @pytest.mark.asyncio
    async def test_insert_flush_integrity_conflicts(self) -> None:
        from sqlalchemy.exc import IntegrityError

        from investigation_agent_platform.infrastructure.persistence.hypothesis_repository import (
            SqlAlchemyHypothesisRepository,
        )

        session = _session_with()
        session.flush = AsyncMock(side_effect=IntegrityError("INSERT", {}, Exception("dup")))
        with patch(f"{APP}.hypothesis_repository.rls_session", return_value=_cm(session)):
            repo = SqlAlchemyHypothesisRepository(db_session_factory=MagicMock())
            with pytest.raises(ConcurrencyError):
                await repo.save("tenant-a", _hyp(), uuid4(), 0)
