"""R12I: code_issue_index session-number race.

One method, one transaction: concurrent allocations are unique and no
session is lost. T01 runs against disposable Postgres; T02 pins the
single-call intake wiring. T03 (the old two-step barrier control) was
removed with the old API by design. Written first, red before the fix.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest


def _code_index_table():  # type: ignore[no-untyped-def]
    from investigation_agent_platform.infrastructure.persistence.models import (
        CodeIssueIndexORM,
    )

    return CodeIssueIndexORM


@pytest.mark.asyncio
async def test_r12i_t01_concurrent_allocations_are_unique(disposable_db) -> None:  # type: ignore[no-untyped-def]
    from investigation_agent_platform.infrastructure.persistence.knowledge_repository import (
        SqlAlchemyCodeIssueIndex,
    )
    from tests.unit.r12_disposable_db import dispose_engine, make_session_factory

    factory, engine = make_session_factory(disposable_db)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                _code_index_table().__table__.create, checkfirst=True
            )
        store = SqlAlchemyCodeIssueIndex(factory)
        now = datetime.now(UTC)
        results = await asyncio.gather(
            *[
                store.allocate_session("fp-race", uuid.uuid4(), now)
                for _ in range(8)
            ]
        )
        assert sorted(results) == [1, 2, 3, 4, 5, 6, 7, 8]
        rows = await store.sessions_for_fingerprint("fp-race")
        assert [r[0] for r in rows] == [1, 2, 3, 4, 5, 6, 7, 8]
    finally:
        await dispose_engine(engine)


def test_r12i_t02_intake_makes_a_single_call() -> None:
    from investigation_agent_platform.application.knowledge.intake import (
        ErrorIntakeService,
    )

    calls: list = []

    class _Repo:
        async def create(self, tenant_id, investigation):
            return investigation

        async def get_by_id(self, tenant_id, investigation_id):
            return None

    class _SessionRepo:
        async def append(self, tenant_id, session):
            return None

    class _Index:
        async def latest_investigation(self, fp):
            return None

        async def sessions_for_fingerprint(self, fp):
            return []

        async def allocate_session(self, fp, iid, now):
            calls.append((fp, iid, now))
            return 1

        # The old split API is gone: any use raises loudly.
        def __getattr__(self, name):
            if name in ("next_session_number", "record_session"):
                raise AttributeError(f"removed API: {name}")
            raise AttributeError(name)

    class _Clock:
        def utcnow(self):
            return datetime.now(UTC)

    async def _run():  # type: ignore[no-untyped-def]
        svc = ErrorIntakeService(
            investigation_repo=_Repo(),
            session_repo=_SessionRepo(),
            code_issue_index=_Index(),
            clock=_Clock(),
        )
        return await svc.intake(
            tenant_id="t",
            application_id="a",
            error_class="E",
            repository="",
            revision="",
            failing_symbol="f",
            problem_description="p",
        )

    result = asyncio.run(_run())
    assert len(calls) == 1
    assert result.session_number == 1
