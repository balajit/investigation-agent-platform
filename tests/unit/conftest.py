# tests/unit/conftest.py
"""Shared test doubles (H14-D3: the conclude path's unit of work)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest


class FakeConclusionUnitOfWork:
    """Test double for the conclude path's unit of work.

    Records the commit call and delegates to the repos' own single-write
    methods (the same three writes, no shared session). Repos resolve from
    the context at call time so tests may attach finding repos after the
    context is built. Tests that need a faulting unit replace or wrap
    ``commit_verdict``.
    """

    def __init__(
        self,
        hypothesis_repo: Any = None,
        finding_repo: Any = None,
        ctx: Any = None,
    ) -> None:
        self._hypothesis_repo = hypothesis_repo
        self._finding_repo = finding_repo
        self._ctx = ctx
        self.calls: list[dict[str, Any]] = []

    def _repos(self) -> tuple[Any, Any]:
        ctx = self._ctx
        hyp_repo = getattr(ctx, "hypothesis_repo", None) if ctx is not None else None
        find_repo = getattr(ctx, "finding_repo", None) if ctx is not None else None
        return hyp_repo or self._hypothesis_repo, find_repo or self._finding_repo

    async def commit_verdict(
        self,
        tenant_id: str,
        *,
        investigation_id: UUID,
        hypothesis: Any | None,
        expected_version: int,
        finding: Any,
        conclusion: Any,
    ) -> None:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "investigation_id": investigation_id,
                "hypothesis": hypothesis,
                "expected_version": expected_version,
                "finding": finding,
                "conclusion": conclusion,
            }
        )
        hyp_repo, find_repo = self._repos()
        if hypothesis is not None and hyp_repo is not None:
            await hyp_repo.save(tenant_id, hypothesis, investigation_id, expected_version)
        if find_repo is not None:
            await find_repo.save_finding_record(tenant_id, finding)
            await find_repo.save_conclusion(tenant_id, conclusion)


# --- R12 disposable Postgres (db-suite) --------------------------------------
# Session container on a non-protected loopback port, started lazily the
# first time an R12 db test requests it; per-test databases are created
# and dropped around each test. Skips cleanly when Docker is unavailable.
# Every URL goes through require_disposable (pg-guard contract dogfooded).


def _r12_docker_ok() -> bool:
    import shutil

    return shutil.which("docker") is not None


def _r12_tcp_ready(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("localhost", port), timeout=1.0):
            return True
    except OSError:
        return False


def _r12_container_running(name: str) -> bool:
    import subprocess

    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return out.returncode == 0 and out.stdout.strip() == "true"
    except Exception:
        return False


def _r12_ensure_container() -> None:
    import subprocess
    import time

    name = "iap-r12-disposable-pg"
    if _r12_container_running(name):
        return
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30, check=False)
    proc = subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-p", "5544:5432",
            "-e", "POSTGRES_USER=iap",
            "-e", "POSTGRES_PASSWORD=iaptest12",
            "postgres:16-alpine",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"disposable postgres did not start: {proc.stderr[-2000:]}")
    deadline = time.time() + 90
    while time.time() < deadline:
        if _r12_tcp_ready(5544):
            return
        time.sleep(0.5)
    raise RuntimeError("disposable postgres never became reachable")


@pytest.fixture(scope="session")
def disposable_postgres():  # type: ignore[no-untyped-def]
    """Base URL of a disposable Postgres; skips when Docker is unavailable."""
    import pytest as _pytest

    if not _r12_docker_ok():
        _pytest.skip("docker unavailable: R12 db-suite needs a disposable postgres")
    try:
        _r12_ensure_container()
    except RuntimeError as exc:
        _pytest.skip(f"disposable postgres unavailable: {exc}")
    from investigation_agent_platform.infrastructure.persistence.pg_guard import (
        require_disposable,
    )

    url = "postgresql://iap:iaptest12@localhost:5544/postgres"
    require_disposable(url, purpose="R12 db-suite")
    return url


@pytest.fixture()
def disposable_db(disposable_postgres):  # type: ignore[no-untyped-def]
    """Fresh empty database per test; dropped afterwards. Yields its URL."""
    import asyncio as _asyncio
    import uuid as _uuid

    import sqlalchemy as _sa
    from sqlalchemy.ext.asyncio import create_async_engine as _create

    from investigation_agent_platform.infrastructure.persistence.pg_guard import (
        require_disposable,
    )

    async def _create_database(base_url: str, dbname: str) -> str:
        engine = _create(
            base_url.replace("postgresql://", "postgresql+asyncpg://", 1),
            isolation_level="AUTOCOMMIT",
        )
        try:
            async with engine.connect() as conn:
                await conn.execute(_sa.text(f'CREATE DATABASE "{dbname}"'))
        finally:
            await engine.dispose()
        return base_url.rsplit("/", 1)[0] + f"/{dbname}"

    async def _drop_database(base_url: str, dbname: str) -> None:
        engine = _create(
            base_url.replace("postgresql://", "postgresql+asyncpg://", 1),
            isolation_level="AUTOCOMMIT",
        )
        try:
            async with engine.connect() as conn:
                await conn.execute(
                    _sa.text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :db AND pid <> pg_backend_pid()"
                    ),
                    {"db": dbname},
                )
                await conn.execute(_sa.text(f'DROP DATABASE IF EXISTS "{dbname}"'))
        except Exception:
            pass
        finally:
            try:
                await engine.dispose()
            except Exception:
                pass

    dbname = f"r12_{_uuid.uuid4().hex[:12]}"
    url = _asyncio.run(_create_database(disposable_postgres, dbname))
    require_disposable(url, purpose="R12 db-suite per-test database")
    try:
        yield url
    finally:
        _asyncio.run(_drop_database(disposable_postgres, dbname))
