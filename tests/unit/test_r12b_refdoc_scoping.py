"""R12B: refdoc retrieval post-filters application_id; the HNSW index is global.

The app filter moves into SQL for both branches and the vector query
uses iterative scan. T01 inspects the compiled statements; T02 proves a
busy sibling no longer starves the answer; T03 (db-suite, pgvector)
documents the recall defect. Written first, red before the fix.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _row(app, rank):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        id=f"{app}-{rank}",
        application_id=app,
        source_id=f"src-{app}",
        document_path=f"/docs/{app}/{rank}.md",
        content=f"content {app} {rank}",
        source_revision="r1",
    )


class _StmtCapture:
    def __init__(self):  # type: ignore[no-untyped-def]
        self.statements: list[str] = []

    async def scalars(self, stmt):  # type: ignore[no-untyped-def]
        self.statements.append(str(stmt))

        class _R:
            def all(self_inner):  # type: ignore[no-untyped-def]
                return []

        return _R()

    async def commit(self):  # type: ignore[no-untyped-def]
        return None

    async def rollback(self):  # type: ignore[no-untyped-def]
        return None

    async def execute(self, stmt, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.statements.append(str(stmt))

        class _R:
            def first(self_inner):  # type: ignore[no-untyped-def]
                return None

        return _R()


def _repo_with(capture):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.infrastructure.persistence.reference_doc_repository import (
        SqlAlchemyReferenceDocRepository,
    )

    class _Factory:
        def __init__(self, session):  # type: ignore[no-untyped-def]
            self._session = session

        def __call__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return capture

        async def __aexit__(self, *exc):  # type: ignore[no-untyped-def]
            return False

    return SqlAlchemyReferenceDocRepository(_Factory(capture))


@pytest.mark.asyncio
async def test_r12b_t01_filter_is_in_the_sql() -> None:
    capture = _StmtCapture()
    repo = _repo_with(capture)
    await repo.lexical_candidates("t", "timeout", 10)
    assert not any("application_id = " in s for s in capture.statements)
    capture.statements.clear()
    await repo.lexical_candidates("t", "timeout", 10, "app-1")
    assert any("application_id = " in s for s in capture.statements)
    capture.statements.clear()
    await repo.vector_candidates("t", [0.0] * 1536, "m", "1.0", 10)
    assert not any("application_id = " in s for s in capture.statements)
    capture.statements.clear()
    await repo.vector_candidates("t", [0.0] * 1536, "m", "1.0", 10, "app-1")
    joined = "\n".join(capture.statements)
    assert "application_id = " in joined
    # Negative control: without the predicate the filter disappears (above).
    assert "hnsw.iterative_scan" in joined


@pytest.mark.asyncio
async def test_r12b_t02_busy_sibling_no_longer_starves() -> None:
    from investigation_agent_platform.application.reference.reference_service import (
        ReferenceDocumentService as ReferenceService,
    )

    app2_rows = [_row("app-2", i) for i in range(40)]
    app1_rows = [_row("app-1", i) for i in range(3)]

    class _FakeRepo:
        def __init__(self):  # type: ignore[no-untyped-def]
            self.seen: list = []

        async def lexical_candidates(self, tenant_id, query, limit, application_id=None):  # type: ignore[no-untyped-def]
            self.seen.append(application_id)
            pool = (app2_rows + app1_rows) if application_id is None else [
                r for r in (app2_rows + app1_rows) if r.application_id == application_id
            ]
            return pool[:30]

        async def vector_candidates(self, tenant_id, vector, model, version, limit, application_id=None):  # type: ignore[no-untyped-def]
            return []

    repo = _FakeRepo()
    service = ReferenceService(chunk_repo=repo, embedder=None)
    hits, _ = await service.search("t", "timeout", application_id="app-1")
    assert repo.seen == ["app-1"]
    assert sorted(h.document_path for h in hits) == [
        "/docs/app-1/0.md", "/docs/app-1/1.md", "/docs/app-1/2.md"
    ]


def _ensure_pgvector_container(name: str, port: int) -> None:
    """Blocking container setup; run in a thread from async tests."""
    import shutil
    import socket as _socket
    import subprocess as _subprocess
    import time as _time

    if shutil.which("docker") is None:
        raise FileNotFoundError("docker unavailable")
    running = _subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if running.returncode == 0 and running.stdout.strip() == "true":
        return
    _subprocess.run(
        ["docker", "rm", "-f", name], capture_output=True, timeout=30, check=False
    )
    proc = _subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name,
         "-p", f"{port}:5432",
         "-e", "POSTGRES_USER=iap", "-e", "POSTGRES_PASSWORD=iaptest12",
         "pgvector/pgvector:pg16"],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pgvector container unavailable: {proc.stderr[-500:]}")
    deadline = _time.time() + 90
    while _time.time() < deadline:
        try:
            with _socket.create_connection(("localhost", port), timeout=1.0):
                return
        except OSError:
            _time.sleep(0.5)
    raise RuntimeError("pgvector container never became reachable")


@pytest.mark.asyncio
async def test_r12b_t03_recall_with_crowded_index() -> None:
    import asyncio as _asyncio

    sqlalchemy = pytest.importorskip("sqlalchemy")
    from sqlalchemy.ext.asyncio import create_async_engine as _create_engine

    name = "iap-r12-disposable-pgvector"
    port = 5545
    try:
        await _asyncio.to_thread(_ensure_pgvector_container, name, port)
    except (FileNotFoundError, RuntimeError) as exc:
        pytest.skip(str(exc))

    try:
        import uuid as _uuid2

        from investigation_agent_platform.infrastructure.persistence.models import (
            ReferenceDocumentChunkORM,
            ReferenceIndexGenerationORM,
        )
        from investigation_agent_platform.infrastructure.persistence.pg_guard import (
            require_disposable,
        )
        from investigation_agent_platform.infrastructure.persistence.reference_doc_repository import (
            SqlAlchemyReferenceDocRepository,
        )

        base = f"postgresql+asyncpg://iap:iaptest12@localhost:{port}/postgres"
        require_disposable(base, purpose="R12B recall battery")
        admin = _create_engine(base, isolation_level="AUTOCOMMIT")
        dbname = f"r12b_{_uuid2.uuid4().hex[:12]}"
        try:
            async with admin.connect() as _conn:
                await _conn.execute(
                    sqlalchemy.text(f'CREATE DATABASE "{dbname}"')
                )
        finally:
            await admin.dispose()
        url = f"postgresql+asyncpg://iap:iaptest12@localhost:{port}/{dbname}"
        engine = _create_engine(url)
        try:
            async with engine.begin() as conn:
                try:
                    await conn.execute(sqlalchemy.text("CREATE EXTENSION IF NOT EXISTS vector"))
                except Exception as exc:
                    await conn.rollback()
                    pytest.skip(f"pgvector absent: {exc}")
                await conn.run_sync(
                    ReferenceIndexGenerationORM.__table__.create, checkfirst=True
                )
                await conn.run_sync(
                    ReferenceDocumentChunkORM.__table__.create, checkfirst=True
                )
                await conn.execute(
                    sqlalchemy.text(
                        "CREATE INDEX IF NOT EXISTS idx_r12b_vector "
                        "ON reference_document_chunks "
                        "USING hnsw (embedding vector_cosine_ops)"
                    )
                )

            from sqlalchemy.ext.asyncio import async_sessionmaker as _sessionmaker

            factory = _sessionmaker(engine, expire_on_commit=False)
            query_vec = [1.0] + [0.0] * 1535
            near_vec = [0.99, 0.01] + [0.0] * 1534
            far_vec = [0.0, 1.0] + [0.0] * 1534

            async def _seed() -> None:
                async with factory() as session:
                    session.add(
                        ReferenceIndexGenerationORM(
                            tenant_id="small",
                            source_id="s1",
                            generation=1,
                            status="ACTIVE",
                            source_revision="r",
                            embedding_model="m",
                            embedding_version="1.0",
                            chunk_count=5,
                        )
                    )
                    for i in range(5):
                        session.add(
                            ReferenceDocumentChunkORM(
                                tenant_id="small",
                                application_id="app-1",
                                source_id="s1",
                                document_path=f"/docs/small/{i}.md",
                                chunk_index=i,
                                content=f"small content {i}",
                                content_hash=f"h-small-{i}",
                                source_revision="r",
                                embedding_model="m",
                                embedding_version="1.0",
                                generation=1,
                                lifecycle="ACTIVE",
                                embedding=list(far_vec),
                            )
                        )
                    for i in range(90):
                        session.add(
                            ReferenceDocumentChunkORM(
                                tenant_id="big",
                                application_id="app-9",
                                source_id="s9",
                                document_path=f"/docs/big/{i}.md",
                                chunk_index=i,
                                content=f"big content {i}",
                                content_hash=f"h-big-{i}",
                                source_revision="r",
                                embedding_model="m",
                                embedding_version="1.0",
                                generation=1,
                                lifecycle="ACTIVE",
                                embedding=list(near_vec),
                            )
                        )
                    await session.commit()

            await _seed()
            repo = SqlAlchemyReferenceDocRepository(factory)
            got = await repo.vector_candidates("small", query_vec, "m", "1.0", 5, "app-1")
            assert len(got) == 5, "iterative scan must return the requested count"
            # Without iterative scan the fixed candidate pool loses to the
            # crowded index: ef_search=4 over mostly other-tenant vectors,
            # then the old post-filter drops everything that is not app-1.
            # The planner is forced onto the HNSW path (a tiny table would
            # otherwise seq-scan exactly) to document the index behavior.
            async with factory() as session:
                await session.execute(sqlalchemy.text("SET LOCAL hnsw.ef_search = 4"))
                await session.execute(sqlalchemy.text("SET LOCAL enable_seqscan = off"))
                await session.execute(sqlalchemy.text("SET LOCAL enable_bitmapscan = off"))
                await session.execute(sqlalchemy.text("SET LOCAL enable_sort = off"))
                raw = await session.execute(
                    sqlalchemy.text(
                        "SELECT application_id FROM reference_document_chunks "
                        "ORDER BY embedding <=> :q LIMIT 30"
                    ),
                    {"q": "[" + ",".join(map(str, query_vec)) + "]"},
                )
                app1 = [r[0] for r in raw.all() if r[0] == "app-1"]
                assert app1 == [], "old post-filter sees none of app-1's rows"
        finally:
            await engine.dispose()
    finally:
        pass
