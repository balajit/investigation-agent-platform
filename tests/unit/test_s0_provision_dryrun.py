"""S0 db dry-run: provision script executes cleanly on disposable Postgres.

Pgvector disposable (port 5545); skips when Docker is unavailable. Creates
the full model schema, runs scripts/provision-onboarding-db-roles.sql
statement by statement, then spot-checks privileges and role attributes.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


def _split_statements(sql: str) -> list[str]:
    """Split on semicolons outside line comments and $$ dollar-quoting."""
    no_comments = "\n".join(
        line.split("--", 1)[0] for line in sql.splitlines()
    )
    parts: list[str] = []
    buf: list[str] = []
    i = 0
    in_dollar = False
    while i < len(no_comments):
        if no_comments.startswith("$$", i):
            in_dollar = not in_dollar
            buf.append("$$")
            i += 2
            continue
        if no_comments[i] == ";" and not in_dollar:
            stmt = "".join(buf).strip()
            if stmt:
                parts.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(no_comments[i])
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


async def _dryrun_url(pytest, sqlalchemy, uuid_mod):  # type: ignore[no-untyped-def]
    import asyncio as _asyncio
    import shutil
    import subprocess

    from sqlalchemy.ext.asyncio import create_async_engine as _create_engine

    from investigation_agent_platform.infrastructure.persistence.pg_guard import (
        require_disposable,
    )

    name = "iap-r12-disposable-pgvector"
    port = 5545
    if shutil.which("docker") is None:
        pytest.skip("docker unavailable: S0 dry-run needs a disposable postgres")
    running = await _asyncio.to_thread(
        subprocess.run,
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if running.stdout.strip() != "true":
        pytest.skip("pgvector disposable container not running")
    base = f"postgresql://iap:iaptest12@localhost:{port}/postgres"
    require_disposable(base, purpose="S0 provision dry-run")
    admin = _create_engine(base.replace("postgresql://", "postgresql+asyncpg://", 1), isolation_level="AUTOCOMMIT")
    dbname = f"s0_{uuid_mod.uuid4().hex[:12]}"
    try:
        async with admin.connect() as conn:
            await conn.execute(sqlalchemy.text(f'CREATE DATABASE "{dbname}"'))
    finally:
        await admin.dispose()
    url = f"postgresql+asyncpg://iap:iaptest12@localhost:{port}/{dbname}"

    async def _drop() -> None:
        engine = _create_engine(
            base.replace("postgresql://", "postgresql+asyncpg://", 1), isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as conn:
                await conn.execute(
                    sqlalchemy.text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :db AND pid <> pg_backend_pid()"),
                    {"db": dbname},
                )
                await conn.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{dbname}"'))
        except Exception:
            pass
        finally:
            await engine.dispose()

    return url, _drop


async def test_s0_t05_script_runs_clean_on_disposable() -> None:
    import uuid as _uuid

    sqlalchemy = pytest.importorskip("sqlalchemy")

    from sqlalchemy.ext.asyncio import create_async_engine

    url, drop = await _dryrun_url(pytest, sqlalchemy, _uuid)
    engine = create_async_engine(url)
    try:
        from investigation_agent_platform.infrastructure.persistence.models import Base

        async with engine.begin() as conn:
            await conn.execute(sqlalchemy.text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all, checkfirst=True)
        from pathlib import Path

        script = (Path(__file__).resolve().parents[2] / "scripts" / "provision-onboarding-db-roles.sql").read_text()
        stmts = _split_statements(script)
        assert len(stmts) > 10
        async with engine.begin() as conn:
            for stmt in stmts:
                await conn.execute(sqlalchemy.text(stmt))

        from sqlalchemy import text

        async def _priv(role: str, table: str, verb: str) -> bool:
            async with engine.connect() as conn:
                row = (await conn.execute(
                    text("SELECT has_table_privilege(:r, :t, :v)"),
                    {"r": role, "t": f"public.{table}", "v": verb},
                )).scalar()
                return bool(row)

        # Spot checks: matrix verbs present where required…
        assert await _priv("iap_app", "investigations", "SELECT")
        assert await _priv("iap_app", "investigations", "DELETE")
        assert await _priv("iap_worker", "evidence", "INSERT")
        assert await _priv("iap_app", "chat_messages", "DELETE")
        assert await _priv("iap_app", "application_profiles", "DELETE")
        # …and absent where the matrix withholds them.
        assert not await _priv("iap_worker", "chat_messages", "SELECT")
        assert not await _priv("iap_worker", "application_profiles", "INSERT")
        assert not await _priv("iap_app", "investigations", "TRUNCATE")

        # S3 migrator-path prerequisites (040 runs as iap_migrator): migrator
        # owns the tables it ALTERs (where present), holds directory
        # membership for the function-owner handoff, and the directory role
        # holds schema CREATE for that handoff.
        async with engine.connect() as conn:
            owner_of = (
                await conn.execute(text(
                    "SELECT tablename, tableowner FROM pg_tables "
                    "WHERE schemaname='public' AND tablename IN "
                    "('analyzer_files','analyzer_symbols','analyzer_edges',"
                    "'analyzer_endpoints','alembic_version')"))
            ).all()
        for table, owner in owner_of:
            assert owner == "iap_migrator", (table, owner)
        async with engine.connect() as conn:
            member = (await conn.execute(text(
                "SELECT count(*) FROM pg_auth_members m "
                "JOIN pg_roles a ON a.oid=m.roleid "
                "JOIN pg_roles b ON b.oid=m.member "
                "WHERE a.rolname='iap_tenant_directory' AND b.rolname='iap_migrator'"))
            ).scalar()
            assert member == 1
            crea = await conn.execute(text(
                "SELECT has_schema_privilege('iap_tenant_directory','public','CREATE')"))
            assert crea.scalar() is True

        async with engine.connect() as conn:
            rows = (await conn.execute(text(
                "SELECT rolname, rolsuper, rolbypassrls, rolcanlogin "
                "FROM pg_roles WHERE rolname IN "
                "('iap_app','iap_worker','iap_migrator','iap_registry_admin','iap_tenant_directory')"
            ))).all()
        by_name = {r[0]: r for r in rows}
        assert set(by_name) == {"iap_app", "iap_worker", "iap_migrator", "iap_registry_admin", "iap_tenant_directory"}
        for name, (_n, sup, bypass, login) in by_name.items():
            assert sup is False and bypass is False, name
            if name == "iap_tenant_directory":
                assert login is False, name
    finally:
        await engine.dispose()
        await drop()
