# tests/unit/test_part11_phase6.py
"""Task 10.b — Reference-document indexing + search tests (Phase 11.6).

Fetcher conformance (git/local/S3), generation reconciliation, isolation,
hybrid retrieval + version routing, golden-set recall, security controls,
REST surface, MCP tool, and the 014 migration chain. No network, no LLM —
embedders and S3 are fakes.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import (
    _DEFAULT_PROFILE,
    AppContext,
    set_app_context,
)
from investigation_agent_platform.application.reference.reference_service import (
    ReferenceDocumentService,
)
from investigation_agent_platform.domain.profile.models import ReferenceDocsProfile
from investigation_agent_platform.domain.reference.documents import (
    REFERENCE_EMBEDDING_DIMS,
)
from investigation_agent_platform.ports.knowledge.embeddings import EmbeddingResult

# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------

_VOCAB = ["timeout", "latency", "database", "auth", "cache", "queue", "memory", "disk"]


def _bow(text: str) -> list[float]:
    vector = [0.0] * REFERENCE_EMBEDDING_DIMS
    low = text.lower()
    for index, word in enumerate(_VOCAB):
        if word in low:
            vector[index] = 1.0
    if not any(vector):
        vector[-1] = 1.0
    return vector


class _FakeEmbedder:
    def __init__(self, model: str = "test-embed") -> None:
        self.model = model
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        self.calls.append(texts)
        return [
            EmbeddingResult(vector=_bow(text), embedding_model=self.model, embedding_version="1.0")
            for text in texts
        ]


def _local_spec(root: str, paths: list[str] | None = None) -> dict:
    return {"kind": "local_path", "root": root, "paths": paths or []}


def _profile(app_id: str, sources: list[dict], roots: list[str] | None = None):
    return _DEFAULT_PROFILE.model_copy(
        update={
            "id": app_id,
            "reference_docs_configuration": ReferenceDocsProfile(
                sources=sources, local_allowed_roots=roots or []
            ),
        }
    )


def _write(root: Path, name: str, content: str) -> None:
    (root / name).write_text(content)


async def _service(ctx: AppContext, embedder=None) -> ReferenceDocumentService:
    return ReferenceDocumentService(
        chunk_repo=ctx.reference_repo, profile_repo=ctx.profile_repo, embedder=embedder
    )


# ---------------------------------------------------------------------------
# fetcher conformance
# ---------------------------------------------------------------------------


class TestGitFetcher:
    def test_clone_local_repo_and_walk(self, tmp_path: Path) -> None:
        import pygit2

        origin = tmp_path / "origin"
        origin.mkdir()
        repo = pygit2.init_repository(str(origin))
        (origin / "docs").mkdir()
        (origin / "docs" / "guide.md").write_text("timeout tuning guide")
        (origin / "notes.txt").write_text("latency notes")
        repo.index.add_all()
        repo.index.write()
        author = pygit2.Signature("t", "t@example.com")
        repo.create_commit("HEAD", author, author, "init", repo.index.write_tree(), [])
        head = str(repo.head.target)

        from investigation_agent_platform.infrastructure.reference.fetchers import (
            GitReferenceFetcher,
        )

        fetcher = GitReferenceFetcher()
        revision = fetcher._clone(str(origin), str(tmp_path / "clone"), "HEAD")
        assert revision == head
        files = fetcher._walk_files(tmp_path / "clone", ["docs"], revision)
        assert [item.path for item in files] == ["docs/guide.md"]
        assert files[0].source_revision == head
        assert files[0].content_hash.startswith("sha256:")

    def test_clone_rejects_non_https(self) -> None:
        import anyio

        from investigation_agent_platform.domain.common.exceptions import (
            CredentialProviderError,
        )
        from investigation_agent_platform.domain.reference.documents import GitSourceSpec
        from investigation_agent_platform.infrastructure.reference.fetchers import (
            GitReferenceFetcher,
        )

        async def _run() -> None:
            with pytest.raises(CredentialProviderError):
                await GitReferenceFetcher().fetch(
                    "tenant-a",
                    GitSourceSpec(repo_url="http://example.com/repo.git", revision="HEAD"),
                )

        anyio.run(_run)

    def test_wrong_spec_type_rejected(self) -> None:
        from investigation_agent_platform.domain.reference.documents import S3SourceSpec
        from investigation_agent_platform.infrastructure.reference.fetchers import (
            GitReferenceFetcher,
        )

        with pytest.raises(TypeError):
            import anyio

            anyio.run(
                GitReferenceFetcher().fetch,
                "tenant-a",
                S3SourceSpec(endpoint="https://s3.example.com", bucket="b", keys=["k"]),
            )


class TestLocalFetcher:
    def test_confined_fetch(self, tmp_path: Path) -> None:
        import anyio

        from investigation_agent_platform.domain.reference.documents import LocalPathSourceSpec
        from investigation_agent_platform.infrastructure.reference.fetchers import (
            LocalPathReferenceFetcher,
        )

        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.md").write_text("timeout story")

        async def _run():
            return await LocalPathReferenceFetcher(allowed_roots=[str(tmp_path)]).fetch(
                "tenant-a", LocalPathSourceSpec(root=str(tmp_path), paths=[])
            )

        files = anyio.run(_run)
        assert {item.path for item in files} >= {"docs/a.md"}

    def test_non_allowlisted_root_rejected(self, tmp_path: Path) -> None:
        import anyio

        from investigation_agent_platform.domain.reference.documents import LocalPathSourceSpec
        from investigation_agent_platform.infrastructure.reference.fetchers import (
            LocalPathReferenceFetcher,
        )

        async def _run():
            await LocalPathReferenceFetcher(allowed_roots=["/nonexistent-root"]).fetch(
                "tenant-a", LocalPathSourceSpec(root=str(tmp_path), paths=[])
            )

        with pytest.raises(RuntimeError):
            anyio.run(_run)

    def test_traversal_and_symlink_rejected(self, tmp_path: Path) -> None:
        import anyio

        from investigation_agent_platform.domain.reference.documents import LocalPathSourceSpec
        from investigation_agent_platform.infrastructure.reference.fetchers import (
            LocalPathReferenceFetcher,
        )

        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        (tmp_path / "link.md").symlink_to(outside)

        async def _run():
            return await LocalPathReferenceFetcher(allowed_roots=[str(tmp_path)]).fetch(
                "tenant-a", LocalPathSourceSpec(root=str(tmp_path), paths=["../escape"])
            )

        with pytest.raises(RuntimeError):
            anyio.run(_run)

        async def _run2():
            return await LocalPathReferenceFetcher(allowed_roots=[str(tmp_path)]).fetch(
                "tenant-a", LocalPathSourceSpec(root=str(tmp_path), paths=[])
            )

        files = anyio.run(_run2)
        assert "link.md" not in {item.path for item in files}
        assert "outside.txt" in {item.path for item in files}

    def test_oversized_and_binary_skipped(self, tmp_path: Path) -> None:
        import anyio

        from investigation_agent_platform.domain.reference.documents import LocalPathSourceSpec
        from investigation_agent_platform.infrastructure.reference.fetchers import (
            LocalPathReferenceFetcher,
        )

        (tmp_path / "big.bin").write_bytes(b"\x00" * 100)
        (tmp_path / "big.txt").write_bytes(b"x" * (1_000_001))
        (tmp_path / "ok.txt").write_text("fine")

        async def _run():
            return await LocalPathReferenceFetcher(allowed_roots=[str(tmp_path)]).fetch(
                "tenant-a", LocalPathSourceSpec(root=str(tmp_path), paths=[])
            )

        files = anyio.run(_run)
        assert {item.path for item in files} == {"ok.txt"}


class TestS3Fetcher:
    def _fake_minio(self, monkeypatch, objects: dict[str, bytes]):  # type: ignore[no-untyped-def]
        import minio

        class _Response:
            def __init__(self, content: bytes) -> None:
                self._content = content

            def read(self, *args: object) -> bytes:
                return self._content

            def close(self) -> None:
                pass

            def release_conn(self) -> None:
                pass

        class _FakeMinio:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def get_bucket_versioning(self, bucket: str):
                return type("V", (), {"status": "Enabled"})()

            def get_object(self, bucket: str, key: str):
                if key not in objects:
                    raise RuntimeError("NoSuchKey")
                return _Response(objects[key])

        monkeypatch.setattr(minio, "Minio", _FakeMinio)

    def test_fetch_keys(self, monkeypatch) -> None:
        import anyio

        from investigation_agent_platform.domain.reference.documents import S3SourceSpec
        from investigation_agent_platform.infrastructure.reference import fetchers as fetchers_mod
        from investigation_agent_platform.infrastructure.reference.fetchers import (
            S3ReferenceFetcher,
        )
        from investigation_agent_platform.infrastructure.security.endpoint_guard import (
            EndpointPolicy,
        )

        self._fake_minio(monkeypatch, {"a.md": b"timeout doc", "b.md": b"\x00binary"})
        # Bypass DNS (covered by the 11.4 suite + the disallowed case below);
        # these tests exercise fetcher logic, not resolution.
        monkeypatch.setattr(fetchers_mod, "validate_endpoint", lambda url, policy, purpose: url)
        policy = EndpointPolicy(
            allowed_hosts=frozenset({"localhost"}),
            require_https=False,
            allow_loopback=True,
        )

        async def _run():
            return await S3ReferenceFetcher(endpoint_policy=policy).fetch(
                "tenant-a",
                S3SourceSpec(
                    endpoint="http://localhost:9000", bucket="docs", keys=["a.md", "b.md"]
                ),
            )

        files = anyio.run(_run)
        assert [item.path for item in files] == ["a.md"]
        assert files[0].source_revision.startswith("s3:docs:")

    def test_key_traversal_rejected(self, monkeypatch) -> None:
        import anyio

        from investigation_agent_platform.domain.reference.documents import S3SourceSpec
        from investigation_agent_platform.infrastructure.reference import fetchers as fetchers_mod
        from investigation_agent_platform.infrastructure.reference.fetchers import (
            S3ReferenceFetcher,
        )
        from investigation_agent_platform.infrastructure.security.endpoint_guard import (
            EndpointPolicy,
        )

        self._fake_minio(monkeypatch, {})
        monkeypatch.setattr(fetchers_mod, "validate_endpoint", lambda url, policy, purpose: url)
        policy = EndpointPolicy(
            allowed_hosts=frozenset({"localhost"}),
            require_https=False,
            allow_loopback=True,
        )

        async def _run():
            await S3ReferenceFetcher(endpoint_policy=policy).fetch(
                "tenant-a",
                S3SourceSpec(endpoint="http://localhost:9000", bucket="docs", keys=["../evil"]),
            )

        with pytest.raises(RuntimeError):
            anyio.run(_run)

    def test_disallowed_endpoint_rejected(self, monkeypatch) -> None:
        import anyio

        from investigation_agent_platform.domain.common.exceptions import (
            CredentialProviderError,
        )
        from investigation_agent_platform.domain.reference.documents import S3SourceSpec
        from investigation_agent_platform.infrastructure.reference.fetchers import (
            S3ReferenceFetcher,
        )

        async def _run():
            await S3ReferenceFetcher().fetch(
                "tenant-a",
                S3SourceSpec(endpoint="http://169.254.169.254", bucket="docs", keys=["a.md"]),
            )

        with pytest.raises(CredentialProviderError):
            anyio.run(_run)

    def test_secret_refs_allowlisted(self, monkeypatch) -> None:

        monkeypatch.setenv("IAP_REFDOC_S3_ACCESS_KEY", "access-value")
        monkeypatch.setenv("IAP_REFDOC_S3_SECRET_KEY", "secret-value")
        from investigation_agent_platform.application.extensions.registries import (
            PluginConfigError,
            resolve_secret,
        )
        from investigation_agent_platform.domain.common.extension import SecretReference

        assert (
            resolve_secret(
                SecretReference(provider="env", name="IAP_REFDOC_S3_ACCESS_KEY"),
                allowed_env_names=frozenset({"IAP_REFDOC_S3_ACCESS_KEY"}),
            )
            == "access-value"
        )
        with pytest.raises(PluginConfigError) as exc_info:
            resolve_secret(
                SecretReference(provider="env", name="HOME"),
                allowed_env_names=frozenset({"IAP_REFDOC_S3_ACCESS_KEY"}),
            )
        assert "HOME" in str(exc_info.value)


# ---------------------------------------------------------------------------
# service: reconciliation + isolation + hybrid
# ---------------------------------------------------------------------------


class TestReferenceService:
    pytestmark = pytest.mark.asyncio

    async def test_index_and_search(self, tmp_path: Path) -> None:
        ctx = AppContext()
        _write(tmp_path, "timeout.md", "timeout tuning and timeout budgets")
        await ctx.profile_repo.save(
            "tenant-a", _profile("app-1", [_local_spec(str(tmp_path))], [str(tmp_path)])
        )
        service = await _service(ctx, _FakeEmbedder())
        sources = await service.list_sources("tenant-a")
        assert len(sources) == 1
        source_id = sources[0]["source_id"]
        assert sources[0]["kind"] == "local_path"
        summary = await service.index_source("tenant-a", source_id, "app-1")
        assert summary.generation == 1
        assert summary.files_seen == 1
        assert summary.chunks_written == 1
        status = await service.index_status("tenant-a", source_id)
        assert status.active_generation == 1
        assert status.chunk_count == 1
        hits, cursor = await service.search("tenant-a", "timeout budgets")
        assert len(hits) == 1
        assert hits[0].document_path == "timeout.md"
        assert cursor is None

    async def test_unchanged_reindex_noop(self, tmp_path: Path) -> None:
        ctx = AppContext()
        _write(tmp_path, "a.md", "stable content here")
        await ctx.profile_repo.save(
            "tenant-a", _profile("app-1", [_local_spec(str(tmp_path))], [str(tmp_path)])
        )
        service = await _service(ctx, _FakeEmbedder())
        source_id = (await service.list_sources("tenant-a"))[0]["source_id"]
        first = await service.index_source("tenant-a", source_id, "app-1")
        second = await service.index_source("tenant-a", source_id, "app-1")
        assert (first.generation, first.chunks_written) == (1, 1)
        assert (second.generation, second.chunks_written) == (1, 0)
        assert second.tombstoned == 0

    async def test_changed_and_deleted_reconciliation(self, tmp_path: Path) -> None:
        ctx = AppContext()
        _write(tmp_path, "keep.md", "keep timeout")
        _write(tmp_path, "gone.md", "gone latency")
        await ctx.profile_repo.save(
            "tenant-a", _profile("app-1", [_local_spec(str(tmp_path))], [str(tmp_path)])
        )
        service = await _service(ctx, _FakeEmbedder())
        source_id = (await service.list_sources("tenant-a"))[0]["source_id"]
        await service.index_source("tenant-a", source_id, "app-1")
        (tmp_path / "gone.md").unlink()
        _write(tmp_path, "keep.md", "keep timeout revised edition")
        _write(tmp_path, "new.md", "new cache notes")
        summary = await service.index_source("tenant-a", source_id, "app-1")
        assert summary.generation == 2
        # Both the deleted file and keep.md's replaced content are tombstoned
        # as explicitly-absent hashes in the new generation.
        assert summary.tombstoned == 2
        hits, _ = await service.search("tenant-a", "latency")
        assert all(hit.document_path != "gone.md" for hit in hits)
        hits, _ = await service.search("tenant-a", "cache")
        assert [hit.document_path for hit in hits] == ["new.md"]

    async def test_failed_run_keeps_prior_generation(self, tmp_path: Path) -> None:
        ctx = AppContext()
        _write(tmp_path, "a.md", "good timeout content")
        await ctx.profile_repo.save(
            "tenant-a", _profile("app-1", [_local_spec(str(tmp_path))], [str(tmp_path)])
        )
        service = await _service(ctx, _FakeEmbedder())
        source_id = (await service.list_sources("tenant-a"))[0]["source_id"]
        await service.index_source("tenant-a", source_id, "app-1")
        (tmp_path / "a.md").unlink()  # root now empty
        (tmp_path / "a.md").mkdir()  # ...replaced by a directory: no usable files
        with pytest.raises(RuntimeError):
            await service.index_source("tenant-a", source_id, "app-1")
        status = await service.index_status("tenant-a", source_id)
        assert status.active_generation == 1
        hits, _ = await service.search("tenant-a", "timeout")
        assert len(hits) == 1

    async def test_tenant_isolation_identical_source_ids(self, tmp_path: Path) -> None:
        ctx = AppContext()
        _write(tmp_path, "shared.md", "shared timeout doc")
        spec = _local_spec(str(tmp_path))
        await ctx.profile_repo.save("tenant-a", _profile("app-1", [spec], [str(tmp_path)]))
        await ctx.profile_repo.save("tenant-b", _profile("app-1", [spec], [str(tmp_path)]))
        service = await _service(ctx, _FakeEmbedder())
        id_a = (await service.list_sources("tenant-a"))[0]["source_id"]
        id_b = (await service.list_sources("tenant-b"))[0]["source_id"]
        assert id_a == id_b  # identical specs → identical ids
        await service.index_source("tenant-a", id_a, "app-1")
        hits_b, _ = await service.search("tenant-b", "timeout")
        assert hits_b == []
        hits_a, _ = await service.search("tenant-a", "timeout")
        assert len(hits_a) == 1

    async def test_application_isolation(self, tmp_path: Path) -> None:
        ctx = AppContext()
        _write(tmp_path, "a.md", "timeout doc")
        spec = _local_spec(str(tmp_path))
        await ctx.profile_repo.save("tenant-a", _profile("app-1", [spec], [str(tmp_path)]))
        await ctx.profile_repo.save("tenant-a", _profile("app-2", [spec], [str(tmp_path)]))
        service = await _service(ctx, _FakeEmbedder())
        id_1 = (await service.list_sources("tenant-a", "app-1"))[0]["source_id"]
        id_2 = (await service.list_sources("tenant-a", "app-2"))[0]["source_id"]
        assert id_1 != id_2
        await service.index_source("tenant-a", id_1, "app-1")
        hits, _ = await service.search("tenant-a", "timeout", application_id="app-2")
        assert hits == []

    async def test_version_routing_never_mixes_spaces(self, tmp_path: Path) -> None:
        ctx = AppContext()
        _write(tmp_path, "a.md", "timeout doc")
        await ctx.profile_repo.save(
            "tenant-a", _profile("app-1", [_local_spec(str(tmp_path))], [str(tmp_path)])
        )
        old = await _service(ctx, _FakeEmbedder(model="embed-v1"))
        source_id = (await old.list_sources("tenant-a"))[0]["source_id"]
        await old.index_source("tenant-a", source_id, "app-1")
        new = await _service(ctx, _FakeEmbedder(model="embed-v2"))
        hits, _ = await new.search("tenant-a", "timeout")
        # embed-v2 vectors do not exist: the vector branch finds nothing from
        # the old space, and lexical still works without mixing.
        assert all(hit.source_revision for hit in hits)
        rows = await ctx.reference_repo.vector_candidates(
            "tenant-a", _bow("timeout"), "embed-v2", "1.0", 10
        )
        assert rows == []

    async def test_rollback_window(self, tmp_path: Path) -> None:
        ctx = AppContext()
        _write(tmp_path, "a.md", "version one timeout")
        await ctx.profile_repo.save(
            "tenant-a", _profile("app-1", [_local_spec(str(tmp_path))], [str(tmp_path)])
        )
        service = await _service(ctx, _FakeEmbedder())
        source_id = (await service.list_sources("tenant-a"))[0]["source_id"]
        await service.index_source("tenant-a", source_id, "app-1")
        _write(tmp_path, "a.md", "version two latency")
        await service.index_source("tenant-a", source_id, "app-1")
        rolled = await service.rollback_generation("tenant-a", source_id, 1)
        assert rolled.active_generation == 1
        hits, _ = await service.search("tenant-a", "timeout")
        assert len(hits) == 1
        with pytest.raises(ValueError):
            await service.rollback_generation("tenant-a", source_id, 99)

    async def test_purge_source(self, tmp_path: Path) -> None:
        ctx = AppContext()
        _write(tmp_path, "a.md", "timeout doc")
        await ctx.profile_repo.save(
            "tenant-a", _profile("app-1", [_local_spec(str(tmp_path))], [str(tmp_path)])
        )
        service = await _service(ctx, _FakeEmbedder())
        source_id = (await service.list_sources("tenant-a"))[0]["source_id"]
        await service.index_source("tenant-a", source_id, "app-1")
        removed = await service.purge_source("tenant-a", source_id)
        assert removed >= 2  # chunks + generation rows
        assert (await service.index_status("tenant-a", source_id)).active_generation == 0

    async def test_unknown_source_fails_closed(self) -> None:
        ctx = AppContext()
        service = await _service(ctx)
        with pytest.raises(ValueError):
            await service.index_source("tenant-a", "no-such-source", "app-1")
        with pytest.raises(ValueError):
            await service.search("tenant-a", "   ")

    async def test_golden_recall(self, tmp_path: Path) -> None:
        """Recall@1 == 1.0: every golden query finds its document first."""
        ctx = AppContext()
        golden = {
            "timeout.md": "timeout budgets and timeout retries for slow backends",
            "latency.md": "latency percentiles p99 latency tracking",
            "database.md": "database connection pool sizing for postgres database",
            "auth.md": "auth token refresh and oauth auth flows",
            "cache.md": "cache invalidation and cache hit ratios",
        }
        for name, content in golden.items():
            _write(tmp_path, name, content)
        await ctx.profile_repo.save(
            "tenant-a", _profile("app-1", [_local_spec(str(tmp_path))], [str(tmp_path)])
        )
        service = await _service(ctx, _FakeEmbedder())
        source_id = (await service.list_sources("tenant-a"))[0]["source_id"]
        await service.index_source("tenant-a", source_id, "app-1")
        queries = {
            "timeout": "timeout.md",
            "latency": "latency.md",
            "database": "database.md",
            "auth": "auth.md",
            "cache": "cache.md",
        }
        for query, expected in queries.items():
            hits, _ = await service.search("tenant-a", query)
            assert hits, query
            assert hits[0].document_path == expected, query


# ---------------------------------------------------------------------------
# durable SQL round-trip (real PostgreSQL when reachable)
# ---------------------------------------------------------------------------


class TestReferenceSqlDurable:
    async def test_sql_roundtrip(self) -> None:
        import os

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from investigation_agent_platform.domain.reference.documents import (
            ReferenceDocumentChunk,
        )
        from investigation_agent_platform.infrastructure.persistence.reference_doc_repository import (
            SqlAlchemyReferenceDocRepository,
        )

        uri = os.environ.get("IAP_DATABASE_URI", "postgresql://iap:iap@localhost:5452/iap")
        if uri.startswith("postgresql://"):
            uri = "postgresql+asyncpg://" + uri[len("postgresql://") :]
        engine = create_async_engine(uri, pool_pre_ping=True)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        # Unique tenant per run: never collides with prior partial runs.
        tenant = f"refdoc-sql-probe-{uuid4().hex[:8]}"
        try:
            repo = SqlAlchemyReferenceDocRepository(factory)
            assert await repo.active_generation(tenant, "s1") == 0
        except Exception as exc:
            await engine.dispose()
            pytest.skip(f"postgres unreachable: {exc}")
        try:
            generation_id = await repo.create_generation(tenant, "s1", 1, "rev-1", "m", "1.0")
            chunk = ReferenceDocumentChunk(
                tenant_id=tenant,
                source_id="s1",
                document_path="a.md",
                content="timeout deep dive",
                embedding_model="m",
            )
            await repo.upsert_chunk(tenant, chunk, _bow("timeout deep dive"))
            assert await repo.active_generation(tenant, "s1") == 1
            lexical = await repo.lexical_candidates(tenant, "timeout", 10)
            assert len(lexical) == 1
            vectors = await repo.vector_candidates(tenant, _bow("timeout"), "m", "1.0", 10)
            assert len(vectors) == 1
            await repo.set_generation_status(tenant, generation_id, "SUPERSEDED", 1)
            assert await repo.active_generation(tenant, "s1") == 0
            removed = await repo.delete_source(tenant, "s1")
            assert removed == 2
        finally:
            await engine.dispose()


TestReferenceSqlDurable.test_sql_roundtrip = pytest.mark.asyncio(
    TestReferenceSqlDurable.test_sql_roundtrip
)


# ---------------------------------------------------------------------------
# REST surface
# ---------------------------------------------------------------------------


class TestReferenceDocsEndpoints:
    def _client(self, monkeypatch) -> tuple[TestClient, AppContext]:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("IAP_ENVIRONMENT", "development")
        ctx = AppContext()
        set_app_context(ctx)
        return TestClient(create_app()), ctx

    def _auth(self, tenant: str = "tenant-a") -> dict[str, str]:
        return {"X-Tenant-ID": tenant}

    def _seed(self, ctx: AppContext, tmp_path: Path, app_id: str = "app-1") -> None:
        import anyio

        _write(tmp_path, "guide.md", "timeout tuning guide")

        async def _save() -> None:
            await ctx.profile_repo.save(
                "tenant-a",
                _profile(app_id, [_local_spec(str(tmp_path))], [str(tmp_path)]),
            )

        anyio.run(_save)

    def _source_id(self, client: TestClient, app_id: str = "app-1") -> str:
        body = client.get(
            "/api/v1/reference-docs/sources",
            headers=self._auth(),
            params={"application_id": app_id},
        ).json()
        assert len(body["items"]) == 1
        return body["items"][0]["source_id"]

    def test_sources_search_status(self, monkeypatch, tmp_path: Path) -> None:
        client, ctx = self._client(monkeypatch)
        self._seed(ctx, tmp_path)
        source_id = self._source_id(client)
        body = client.get(
            "/api/v1/reference-docs/search",
            headers=self._auth(),
            params={"q": "timeout"},
        ).json()
        assert body["items"] == []  # not indexed yet
        assert body["has_more"] is False
        status = client.get(
            "/api/v1/reference-docs/status",
            headers=self._auth(),
            params={"source_id": source_id},
        ).json()
        assert status["active_generation"] == 0

    def test_reindex_requires_key_replay_conflict(self, monkeypatch, tmp_path: Path) -> None:
        client, ctx = self._client(monkeypatch)
        self._seed(ctx, tmp_path)
        source_id = self._source_id(client)
        assert (
            client.post(
                "/api/v1/reference-docs/reindex",
                json={"application_id": "app-1", "source_id": source_id},
                headers=self._auth(),
            ).status_code
            == 400
        )
        ctx.temporal_client = MagicMock()
        ctx.temporal_client.start_workflow = AsyncMock()
        headers = {**self._auth(), "X-Idempotency-Key": "reindex-1"}
        payload = {"application_id": "app-1", "source_id": source_id}
        first = client.post("/api/v1/reference-docs/reindex", json=payload, headers=headers)
        assert first.status_code == 202, first.text
        assert first.json()["workflow_id"].startswith("wf-reindex-")
        second = client.post("/api/v1/reference-docs/reindex", json=payload, headers=headers)
        assert second.json()["job_id"] == first.json()["job_id"]
        conflict = client.post(
            "/api/v1/reference-docs/reindex",
            json={"application_id": "app-1", "source_id": "other"},
            headers=headers,
        )
        assert conflict.status_code == 409

    def test_reindex_no_temporal_503_and_failure_502(self, monkeypatch, tmp_path: Path) -> None:
        client, ctx = self._client(monkeypatch)
        self._seed(ctx, tmp_path)
        source_id = self._source_id(client)
        payload = {"application_id": "app-1", "source_id": source_id}
        assert (
            client.post(
                "/api/v1/reference-docs/reindex",
                json=payload,
                headers={**self._auth(), "X-Idempotency-Key": "reindex-503"},
            ).status_code
            == 503
        )
        ctx.temporal_client = MagicMock()
        ctx.temporal_client.start_workflow = AsyncMock(side_effect=RuntimeError("down"))
        assert (
            client.post(
                "/api/v1/reference-docs/reindex",
                json=payload,
                headers={**self._auth(), "X-Idempotency-Key": "reindex-502"},
            ).status_code
            == 502
        )

    def test_rollback_and_delete(self, monkeypatch, tmp_path: Path) -> None:
        import anyio

        client, ctx = self._client(monkeypatch)
        self._seed(ctx, tmp_path)
        source_id = self._source_id(client)
        service = ReferenceDocumentService(
            chunk_repo=ctx.reference_repo, profile_repo=ctx.profile_repo
        )

        async def _index() -> None:
            await service.index_source("tenant-a", source_id, "app-1")

        anyio.run(_index)
        rolled = client.post(
            "/api/v1/reference-docs/rollback",
            json={"application_id": "app-1", "source_id": source_id, "generation": 1},
            headers=self._auth(),
        )
        assert rolled.status_code == 200, rolled.text
        assert rolled.json()["active_generation"] == 1
        assert (
            client.post(
                "/api/v1/reference-docs/rollback",
                json={"application_id": "app-1", "source_id": source_id, "generation": 9},
                headers=self._auth(),
            ).status_code
            == 404
        )
        deleted = client.delete(
            f"/api/v1/reference-docs/sources/{source_id}", headers=self._auth()
        ).json()
        assert deleted["status"] == "DELETED"
        assert deleted["rows_removed"] >= 2
        assert (
            client.get(
                "/api/v1/reference-docs/search",
                headers=self._auth("tenant-b"),
                params={"q": "timeout"},
            ).json()["items"]
            == []
        )

    def test_search_validation(self, monkeypatch, tmp_path: Path) -> None:
        client, ctx = self._client(monkeypatch)
        self._seed(ctx, tmp_path)
        assert client.get("/api/v1/reference-docs/search", headers=self._auth()).status_code == 422


# ---------------------------------------------------------------------------
# MCP tool
# ---------------------------------------------------------------------------


class TestReferenceDocsMcpTool:
    @pytest.mark.asyncio
    async def test_core_search_and_unavailable(self, tmp_path: Path) -> None:
        from investigation_agent_platform.application.investigation.context import (
            InvestigationScope,
        )
        from investigation_agent_platform.mcp.tools.reference_docs import (
            search_reference_docs_core,
        )

        ctx = AppContext()
        _write(tmp_path, "auth.md", "auth token refresh guide")
        await ctx.profile_repo.save(
            "tenant-a", _profile("app-1", [_local_spec(str(tmp_path))], [str(tmp_path)])
        )
        service = ReferenceDocumentService(
            chunk_repo=ctx.reference_repo, profile_repo=ctx.profile_repo, embedder=_FakeEmbedder()
        )
        sources = await service.list_sources("tenant-a")
        await service.index_source("tenant-a", sources[0]["source_id"], "app-1")
        services = MagicMock()
        services.reference_docs = service
        scope = InvestigationScope.create(tenant_id="tenant-a", investigation_id=uuid4())
        output = await search_reference_docs_core(
            services=services, scope=scope, q="auth token", kinds=None, limit=10, cursor=None
        )
        assert len(output.items) == 1
        assert output.items[0].document_path == "auth.md"
        assert output.has_more is False
        services_none = MagicMock()
        services_none.reference_docs = None
        envelope = await search_reference_docs_core(
            services=services_none, scope=scope, q="auth", kinds=None, limit=10, cursor=None
        )
        assert hasattr(envelope, "error")

    def test_tool_registered_with_strict_surface(self) -> None:
        from investigation_agent_platform.mcp.registry import TOOL_ARGUMENT_PROPERTIES

        assert TOOL_ARGUMENT_PROPERTIES["search_reference_docs"] == frozenset(
            {"q", "kinds", "limit", "cursor"}
        )


# ---------------------------------------------------------------------------
# migration 014
# ---------------------------------------------------------------------------


class TestMigration014:
    def test_014_chain(self) -> None:
        import importlib.util
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[2]
            / "migrations"
            / "versions"
            / "014_reference_documents.py"
        )
        assert path.is_file()
        spec = importlib.util.spec_from_file_location("migration_014", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.revision == "014_reference_documents"
        assert mod.down_revision == "013_chat_sessions"

    def test_014_is_ancestor_of_head(self) -> None:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config("alembic.ini")
        script = ScriptDirectory.from_config(cfg)
        # 014 is no longer the head (015 superseded it); it must remain a
        # linear ancestor of the current single head.
        rev = script.get_revision(script.get_heads()[0])
        seen = []
        while rev is not None:
            seen.append(rev.revision)
            down = rev.down_revision
            rev = script.get_revision(down) if down else None
        assert "014_reference_documents" in seen
        assert seen.index("015_batch_intake") < seen.index("014_reference_documents")
