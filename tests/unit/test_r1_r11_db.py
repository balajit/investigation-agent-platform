"""R1 db-suite (T14/T15) + R11 control (T13).

Disposable Postgres; skips when Docker is unavailable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

FP = "hk1:" + "a" * 64
OTHER_FP = "hk1:" + "b" * 64


def _ddl() -> list[str]:
    return [
        """CREATE TABLE knowledge_artifacts (
        id UUID PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        application_id TEXT NOT NULL,
        investigation_id UUID NOT NULL,
        kind TEXT NOT NULL,
        statement TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'ACTIVE',
        visibility TEXT NOT NULL DEFAULT 'TENANT',
        code_issue_fingerprint TEXT,
        source_evidence_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
        source_log_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
        valid_to TIMESTAMPTZ
    )""",
        "ALTER TABLE knowledge_artifacts ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE knowledge_artifacts FORCE ROW LEVEL SECURITY",
    ]


def _policy_039() -> list[str]:
    return [
        "DROP POLICY IF EXISTS knowledge_visibility ON knowledge_artifacts",
        """CREATE POLICY knowledge_tenant_all ON knowledge_artifacts
        FOR ALL USING (tenant_id = current_setting('app.tenant_id', true))
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true))""",
        """CREATE POLICY knowledge_shared_read ON knowledge_artifacts
        FOR SELECT USING (
            visibility = 'SHARED_CODE_ISSUE' AND status = 'ACTIVE'
            AND code_issue_fingerprint = ANY (
                string_to_array(coalesce(current_setting('app.shared_fingerprints', true), ''), ',')))""",
    ]


@pytest.mark.asyncio
async def test_r1_t14_db_enforces_membership_proof(disposable_db) -> None:  # type: ignore[no-untyped-def]
    import sqlalchemy as sa
    from r12_disposable_db import dispose_engine, make_session_factory
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    factory, engine = make_session_factory(disposable_db)
    try:
        async with engine.begin() as conn:
            for stmt in _ddl():
                await conn.execute(sa.text(stmt))
        row_id = uuid.uuid4()
        async with factory() as session:
            await session.execute(
                sa.text(
                    "INSERT INTO knowledge_artifacts "
                    "(id, tenant_id, application_id, investigation_id, kind, statement, "
                    "status, visibility, code_issue_fingerprint, valid_to) "
                    "VALUES (:id, 'tenant-a', 'app-1', :iid, 'error_signature', "
                    "'Code issue: E at f (r@rev)', 'ACTIVE', 'SHARED_CODE_ISSUE', :fp, :vt)"
                ),
                {
                    "id": row_id,
                    "iid": uuid.uuid4(),
                    "fp": FP,
                    "vt": datetime.now(UTC) + timedelta(days=1),
                },
            )
            await session.commit()
        async with engine.begin() as conn:
            for stmt in _policy_039():
                await conn.execute(sa.text(stmt))
            await conn.execute(sa.text("DROP ROLE IF EXISTS iap_r1test"))
            await conn.execute(sa.text("CREATE ROLE iap_r1test LOGIN PASSWORD 'iaptest12' NOBYPASSRLS"))
            await conn.execute(
                sa.text(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge_artifacts TO iap_r1test"
                )
            )
        from urllib.parse import urlparse, urlunparse

        parts = urlparse(disposable_db)
        role_url = urlunparse(parts._replace(netloc=f"iap_r1test:iaptest12@{parts.hostname}:{parts.port}"))
        role_url = role_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        role_engine = create_async_engine(role_url)
        role_factory = async_sessionmaker(role_engine, expire_on_commit=False)
        try:
            from sqlalchemy import text

            async def _visible(tenant: str, proofs: list[str]) -> list:
                async with role_factory() as session:
                    await session.execute(text(f"SET LOCAL app.tenant_id = '{tenant}'"))
                    if proofs:
                        await session.execute(
                            text(f"SET LOCAL app.shared_fingerprints = '{','.join(proofs)}'")
                        )
                    rows = (
                        await session.execute(
                            text("SELECT id FROM knowledge_artifacts WHERE id = :id"),
                            {"id": row_id},
                        )
                    ).all()
                    await session.rollback()
                    return rows

            assert len(await _visible("tenant-b", [FP])) == 1
            assert await _visible("tenant-b", []) == []
            assert await _visible("tenant-b", [OTHER_FP]) == []
        finally:
            await dispose_engine(role_engine)
    finally:
        await dispose_engine(engine)


@pytest.mark.asyncio
async def test_r1_t15_cross_tenant_write_blocked(disposable_db) -> None:  # type: ignore[no-untyped-def]
    import sqlalchemy as sa
    from r12_disposable_db import dispose_engine, make_session_factory
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    factory, engine = make_session_factory(disposable_db)
    try:
        async with engine.begin() as conn:
            for stmt in _ddl():
                await conn.execute(sa.text(stmt))
        row_id = uuid.uuid4()
        async with factory() as session:
            await session.execute(
                sa.text(
                    "INSERT INTO knowledge_artifacts "
                    "(id, tenant_id, application_id, investigation_id, kind, statement, "
                    "status, visibility, code_issue_fingerprint) "
                    "VALUES (:id, 'tenant-a', 'app-1', :iid, 'error_signature', "
                    "'Code issue: E at f (r@rev)', 'ACTIVE', 'SHARED_CODE_ISSUE', :fp)"
                ),
                {"id": row_id, "iid": uuid.uuid4(), "fp": FP},
            )
            await session.commit()
        async with engine.begin() as conn:
            for stmt in _policy_039():
                await conn.execute(sa.text(stmt))
            await conn.execute(sa.text("DROP ROLE IF EXISTS iap_r1test"))
            await conn.execute(sa.text("CREATE ROLE iap_r1test LOGIN PASSWORD 'iaptest12' NOBYPASSRLS"))
            await conn.execute(
                sa.text(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge_artifacts TO iap_r1test"
                )
            )
        from urllib.parse import urlparse, urlunparse

        parts = urlparse(disposable_db)
        role_url = urlunparse(parts._replace(netloc=f"iap_r1test:iaptest12@{parts.hostname}:{parts.port}"))
        role_url = role_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        role_engine = create_async_engine(role_url)
        role_factory = async_sessionmaker(role_engine, expire_on_commit=False)
        try:
            from sqlalchemy import text

            async with role_factory() as session:
                await session.execute(text("SET LOCAL app.tenant_id = 'tenant-b'"))
                await session.execute(text(f"SET LOCAL app.shared_fingerprints = '{FP}'"))
                res = await session.execute(
                    text("DELETE FROM knowledge_artifacts WHERE id = :id"), {"id": row_id}
                )
                assert (res.rowcount or 0) == 0
                res = await session.execute(
                    text("UPDATE knowledge_artifacts SET status='X' WHERE id = :id"),
                    {"id": row_id},
                )
                assert (res.rowcount or 0) == 0
                await session.rollback()
            async with role_factory() as session:
                await session.execute(text("SET LOCAL app.tenant_id = 'tenant-a'"))
                res = await session.execute(
                    text("DELETE FROM knowledge_artifacts WHERE id = :id"), {"id": row_id}
                )
                assert (res.rowcount or 0) == 1
                await session.commit()
        finally:
            await dispose_engine(role_engine)
    finally:
        await dispose_engine(engine)


@pytest.mark.asyncio
async def test_r11_t13_test_role_is_not_privileged(disposable_db) -> None:  # type: ignore[no-untyped-def]
    from r12_disposable_db import dispose_engine, make_session_factory

    from investigation_agent_platform.infrastructure.persistence.role_guard import (
        inspect_runtime_role,
    )

    factory, engine = make_session_factory(disposable_db)
    try:
        report = await inspect_runtime_role(engine)
        assert report is not None
        # Disposable runs as superuser by construction: the control proves
        # the inspection sees privilege (so a non-superuser pass is meaningful).
        assert report.superuser is True or report.bypass_rls is True
    finally:
        await dispose_engine(engine)
