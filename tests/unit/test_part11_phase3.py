# tests/unit/test_part11_phase3.py
"""Part 11 Phase 11.3 tests: shared operational foundations.

Covers: artifact key validation + stores, background job repos, quotas,
scoped idempotency, signed cursors, provenance, lifecycle/audit models, jobs
API + WebSocket, hub fan-out, Kafka consumer wiring (TestKafkaBroker),
schedule reconciler + history policy, telemetry helpers, migration 010, and
live-gated Postgres (IAP_RUN_CONTAINER_TESTS=1, skips when unreachable).
"""

from __future__ import annotations

import os
import socket
import threading
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from fastapi.websockets import WebSocketDisconnect

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import (
    AppContext,
    InMemoryArtifactStore,
    InMemoryBackgroundJobRepository,
    set_app_context,
)
from investigation_agent_platform.domain.common.background_job import (
    BackgroundJob,
    BackgroundJobStatus,
    JobProgressEvent,
)
from investigation_agent_platform.domain.common.extension import CapabilityScope

RUN_CONTAINERS = os.environ.get("IAP_RUN_CONTAINER_TESTS", "0") == "1"

requires_containers = pytest.mark.skipif(
    not RUN_CONTAINERS,
    reason="Container tests opt-in only (IAP_RUN_CONTAINER_TESTS=1)",
)

PG_URL = os.environ.get("IAP_TEST_PGVECTOR_URL", "postgresql://iap:iap@localhost:5432/iap_test")


@pytest.fixture
def _restore_context():
    import investigation_agent_platform.api.dependencies as deps

    prev = deps._context
    yield
    deps._context = prev


def _scope(tenant: str = "tenant-a", app: str | None = "app-1") -> CapabilityScope:
    return CapabilityScope(tenant_id=tenant, application_id=app)


def _job(tenant: str = "tenant-a", **overrides) -> BackgroundJob:
    params: dict = {
        "tenant_id": tenant,
        "application_id": "app-1",
        "kind": "test-kind",
        "created_by": "tester",
    }
    params.update(overrides)
    return BackgroundJob(**params)


def _event(job_id, status=BackgroundJobStatus.RUNNING, **overrides) -> JobProgressEvent:
    params: dict = {
        "job_id": job_id,
        "tenant_id": "tenant-a",
        "kind": "test-kind",
        "status": status,
        "progress": 1,
        "total": 4,
    }
    params.update(overrides)
    return JobProgressEvent(**params)


@asynccontextmanager
async def _mock_rls(module_path: str, session: MagicMock, tenant_id: str = "tenant-a"):
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)
    with patch(f"{module_path}.rls_session", return_value=cm) as mock_rls:
        yield session
        mock_rls.assert_called()
        assert mock_rls.call_args.args[1] == tenant_id


def _mock_session(scalars_first=None, scalars_all=None, scalar_value=None) -> MagicMock:
    session = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.first.return_value = scalars_first
    mock_scalars.all.return_value = scalars_all or []
    session.scalars = AsyncMock(return_value=mock_scalars)
    session.scalar = AsyncMock(return_value=scalar_value)
    session.execute = AsyncMock(return_value=MagicMock())
    session.add = MagicMock()
    return session


# ===========================================================================
# Artifact keys
# ===========================================================================


class TestArtifactKeys:
    def test_valid_key(self) -> None:
        from investigation_agent_platform.infrastructure.artifacts.keys import (
            build_object_key,
        )

        assert build_object_key("t-a", "app", "reports/r1.html") == "t-a/app/reports/r1.html"

    def test_none_app_normalized(self) -> None:
        from investigation_agent_platform.infrastructure.artifacts.keys import (
            build_object_key,
        )

        assert build_object_key("t-a", None, "r.html") == "t-a/-/r.html"

    def test_traversal_rejected(self) -> None:
        from investigation_agent_platform.infrastructure.artifacts.keys import (
            build_object_key,
        )

        for bad in ("../evil", "a/../../evil", "..", ".", "/abs", "a\\b", "", "a//b"):
            with pytest.raises(ValueError):
                build_object_key("t-a", "app", bad)

    def test_scope_segments_rejected(self) -> None:
        from investigation_agent_platform.infrastructure.artifacts.keys import (
            build_object_key,
        )

        with pytest.raises(ValueError):
            build_object_key("../evil", "app", "ok")
        with pytest.raises(ValueError):
            build_object_key("t-a", "a/b", "ok")

    def test_scopes_isolate(self) -> None:
        from investigation_agent_platform.infrastructure.artifacts.keys import (
            build_object_key,
        )

        assert build_object_key("t-a", "app", "r") != build_object_key("t-b", "app", "r")


# ===========================================================================
# Local artifact store
# ===========================================================================


class TestLocalArtifactStore:
    def _store(self, tmp_path) -> object:  # type: ignore[no-untyped-def]
        from investigation_agent_platform.infrastructure.artifacts.local_store import (
            LocalArtifactStore,
        )

        return LocalArtifactStore(tmp_path / "artifacts")

    @pytest.mark.asyncio
    async def test_put_get_round_trip(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        store = self._store(tmp_path)
        ref = await store.put(_scope(), "r.html", b"<html/>", "text/html")
        assert ref.size_bytes == 7
        assert ref.content_digest.startswith("sha256:")
        obj = await store.get(_scope(), ref)
        assert obj.content == b"<html/>"

    @pytest.mark.asyncio
    async def test_get_missing_raises(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from investigation_agent_platform.ports.artifacts.store import ArtifactRef

        store = self._store(tmp_path)
        with pytest.raises(FileNotFoundError):
            await store.get(_scope(), ArtifactRef(artifact_id=uuid4(), key="nope"))

    @pytest.mark.asyncio
    async def test_delete_idempotent(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        store = self._store(tmp_path)
        ref = await store.put(_scope(), "d.bin", b"data", "application/octet-stream")
        await store.delete(_scope(), ref)
        await store.delete(_scope(), ref)
        with pytest.raises(FileNotFoundError):
            await store.get(_scope(), ref)

    @pytest.mark.asyncio
    async def test_list_prefix_and_tenant_exclusion(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        store = self._store(tmp_path)
        await store.put(_scope("tenant-a"), "reports/a.html", b"a", "text/html")
        await store.put(_scope("tenant-a"), "other/b.html", b"b", "text/html")
        await store.put(_scope("tenant-b"), "reports/c.html", b"c", "text/html")
        page = await store.list(_scope("tenant-a"), "reports/")
        assert [i.key for i in page.items] == ["reports/a.html"]
        assert page.has_more is False

    @pytest.mark.asyncio
    async def test_signed_url_confined(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        store = self._store(tmp_path)
        ref = await store.put(_scope(), "s.html", b"s", "text/html")
        url = await store.signed_url(_scope(), ref)
        assert url.startswith("file://")
        assert "tenant-a" in url

    @pytest.mark.asyncio
    async def test_oversize_and_metadata_limits(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from unittest.mock import patch as _patch

        store = self._store(tmp_path)
        with _patch(
            "investigation_agent_platform.infrastructure.artifacts.local_store.MAX_ARTIFACT_BYTES",
            4,
        ):
            with pytest.raises(ValueError):
                await store.put(_scope(), "big", b"12345", "text/plain")
        with pytest.raises(ValueError):
            await store.put(_scope(), "m", b"x", "text/plain", {f"k{i}": "v" for i in range(25)})


# ===========================================================================
# In-memory artifact store
# ===========================================================================


class TestInMemoryArtifactStore:
    @pytest.mark.asyncio
    async def test_round_trip_and_cross_tenant_miss(self) -> None:
        store = InMemoryArtifactStore()
        ref = await store.put(_scope("tenant-a"), "r", b"bytes", "text/plain")
        assert (await store.get(_scope("tenant-a"), ref)).content == b"bytes"
        with pytest.raises(FileNotFoundError):
            await store.get(_scope("tenant-b"), ref)

    @pytest.mark.asyncio
    async def test_delete_then_signed_url_missing(self) -> None:
        store = InMemoryArtifactStore()
        ref = await store.put(_scope(), "r", b"bytes", "text/plain")
        assert (await store.signed_url(_scope(), ref)).startswith("memory://")
        await store.delete(_scope(), ref)
        with pytest.raises(FileNotFoundError):
            await store.signed_url(_scope(), ref)


# ===========================================================================
# S3 adapter: offline construction + key validation (no network)
# ===========================================================================


class TestS3AdapterOffline:
    def test_key_validation_before_network(self) -> None:
        import asyncio

        from investigation_agent_platform.infrastructure.artifacts.s3_store import (
            S3ArtifactStore,
        )

        store = S3ArtifactStore(
            endpoint_url="http://localhost:9000",
            bucket_name="test-bucket",
            access_key="user",
            secret_key="pass",
        )
        with pytest.raises(ValueError):
            asyncio.run(store.put(_scope(), "../evil", b"x", "text/plain"))

    def test_secure_flag_from_scheme(self) -> None:
        from investigation_agent_platform.infrastructure.artifacts.s3_store import (
            S3ArtifactStore,
        )

        plain = S3ArtifactStore(
            endpoint_url="http://localhost:9000",
            bucket_name="b",
            access_key="u",
            secret_key="p",
        )
        secure = S3ArtifactStore(
            endpoint_url="https://localhost:9000",
            bucket_name="b",
            access_key="u",
            secret_key="p",
        )
        assert plain._client._base_url.is_https is False
        assert secure._client._base_url.is_https is True


# ===========================================================================
# Background job repositories
# ===========================================================================


class TestInMemoryBackgroundJobRepo:
    @pytest.mark.asyncio
    async def test_create_get_round_trip(self) -> None:
        repo = InMemoryBackgroundJobRepository()
        job = _job()
        await repo.create(job)
        got = await repo.get_by_id("tenant-a", job.id)
        assert got is not None
        assert got.kind == "test-kind"
        assert got.version == 1

    @pytest.mark.asyncio
    async def test_cross_tenant_miss(self) -> None:
        repo = InMemoryBackgroundJobRepository()
        job = _job()
        await repo.create(job)
        assert await repo.get_by_id("tenant-b", job.id) is None

    @pytest.mark.asyncio
    async def test_save_occ(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import ConcurrencyError

        repo = InMemoryBackgroundJobRepository()
        job = _job()
        await repo.create(job)
        updated = job.model_copy(update={"progress": 2, "version": 2})
        await repo.save("tenant-a", updated, expected_version=1)
        assert (await repo.get_by_id("tenant-a", job.id)).progress == 2
        stale = job.model_copy(update={"progress": 3, "version": 2})
        with pytest.raises(ConcurrencyError):
            await repo.save("tenant-a", stale, expected_version=1)
        with pytest.raises(ConcurrencyError):
            await repo.save("tenant-b", updated, expected_version=2)

    @pytest.mark.asyncio
    async def test_list_filters_and_pagination(self) -> None:
        repo = InMemoryBackgroundJobRepository()
        for i in range(3):
            await repo.create(_job(kind="k1"))
        await repo.create(_job(kind="k2", status=BackgroundJobStatus.DONE))
        items, total = await repo.list_jobs("tenant-a", kind="k1")
        assert total == 3
        page, _ = await repo.list_jobs("tenant-a", limit=2, offset=2)
        assert len(page) == 2
        done, done_total = await repo.list_jobs("tenant-a", status=BackgroundJobStatus.DONE)
        assert done_total == 1
        assert done[0].kind == "k2"
        assert (await repo.list_jobs("tenant-b"))[1] == 0


class TestSqlBackgroundJobRepo:
    @pytest.mark.asyncio
    async def test_create_adds_scoped(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.background_job_repository import (
            SqlAlchemyBackgroundJobRepository,
        )

        session = _mock_session()
        async with _mock_rls(
            "investigation_agent_platform.infrastructure.persistence.background_job_repository",
            session,
        ):
            repo = SqlAlchemyBackgroundJobRepository(db_session_factory=MagicMock())
            await repo.create(_job())
            assert session.add.called

    @pytest.mark.asyncio
    async def test_get_empty_scoped(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.background_job_repository import (
            SqlAlchemyBackgroundJobRepository,
        )

        session = _mock_session(scalars_first=None)
        async with _mock_rls(
            "investigation_agent_platform.infrastructure.persistence.background_job_repository",
            session,
        ):
            repo = SqlAlchemyBackgroundJobRepository(db_session_factory=MagicMock())
            assert await repo.get_by_id("tenant-a", uuid4()) is None

    @pytest.mark.asyncio
    async def test_save_conflict_rolls_back(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import ConcurrencyError
        from investigation_agent_platform.infrastructure.persistence.background_job_repository import (
            SqlAlchemyBackgroundJobRepository,
        )

        session = _mock_session()
        session.execute = AsyncMock(return_value=MagicMock(rowcount=0))
        session.rollback = AsyncMock()
        async with _mock_rls(
            "investigation_agent_platform.infrastructure.persistence.background_job_repository",
            session,
        ):
            repo = SqlAlchemyBackgroundJobRepository(db_session_factory=MagicMock())
            job = _job()
            with pytest.raises(ConcurrencyError):
                await repo.save("tenant-a", job, expected_version=9)
            assert session.rollback.called

    @pytest.mark.asyncio
    async def test_list_empty(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.background_job_repository import (
            SqlAlchemyBackgroundJobRepository,
        )

        session = _mock_session(scalars_all=[], scalar_value=0)
        async with _mock_rls(
            "investigation_agent_platform.infrastructure.persistence.background_job_repository",
            session,
        ):
            repo = SqlAlchemyBackgroundJobRepository(db_session_factory=MagicMock())
            items, total = await repo.list_jobs("tenant-a", kind="k1")
            assert items == []
            assert total == 0


# ===========================================================================
# Quotas
# ===========================================================================


class TestQuotaPolicy:
    def test_defaults(self) -> None:
        from investigation_agent_platform.domain.common.quotas import QuotaPolicy

        policy = QuotaPolicy()
        assert policy.max_batch_records == 500
        assert policy.max_concurrent_jobs == 5


class TestInMemoryQuota:
    @pytest.mark.asyncio
    async def test_allow_then_exhaust_then_release(self) -> None:
        from investigation_agent_platform.domain.common.quotas import QuotaPolicy
        from investigation_agent_platform.infrastructure.persistence.quota_enforcer import (
            InMemoryQuotaEnforcer,
        )

        enforcer = InMemoryQuotaEnforcer()
        policy = QuotaPolicy(max_concurrent_jobs=1)
        scope = _scope()
        first = await enforcer.check(scope, "job.dispatch", policy)
        assert first.allowed is True
        second = await enforcer.check(scope, "job.dispatch", policy)
        assert second.allowed is False
        assert second.violated_limit == "max_concurrent_jobs"
        assert second.retry_after_seconds == 60
        await enforcer.release(scope, "job.dispatch")
        third = await enforcer.check(scope, "job.dispatch", policy)
        assert third.allowed is True

    @pytest.mark.asyncio
    async def test_scope_isolation(self) -> None:
        from investigation_agent_platform.domain.common.quotas import QuotaPolicy
        from investigation_agent_platform.infrastructure.persistence.quota_enforcer import (
            InMemoryQuotaEnforcer,
        )

        enforcer = InMemoryQuotaEnforcer()
        policy = QuotaPolicy(max_concurrent_jobs=1)
        assert (await enforcer.check(_scope("tenant-a"), "job.dispatch", policy)).allowed
        assert (await enforcer.check(_scope("tenant-b"), "job.dispatch", policy)).allowed

    @pytest.mark.asyncio
    async def test_concurrent_race(self) -> None:
        import asyncio

        from investigation_agent_platform.domain.common.quotas import QuotaPolicy
        from investigation_agent_platform.infrastructure.persistence.quota_enforcer import (
            InMemoryQuotaEnforcer,
        )

        enforcer = InMemoryQuotaEnforcer()
        policy = QuotaPolicy(max_concurrent_jobs=3)
        scope = _scope()
        results = await asyncio.gather(
            *[enforcer.check(scope, "job.dispatch", policy) for _ in range(10)]
        )
        assert sum(1 for r in results if r.allowed) == 3
        assert sum(1 for r in results if not r.allowed) == 7


class TestPostgresQuota:
    @pytest.mark.asyncio
    async def test_check_increments_scoped(self) -> None:
        from investigation_agent_platform.domain.common.quotas import QuotaPolicy
        from investigation_agent_platform.infrastructure.persistence.quota_enforcer import (
            PostgresQuotaEnforcer,
        )

        session = _mock_session(scalars_first=None)
        async with _mock_rls(
            "investigation_agent_platform.infrastructure.persistence.quota_enforcer",
            session,
        ):
            enforcer = PostgresQuotaEnforcer(db_session_factory=MagicMock())
            result = await enforcer.check(_scope(), "job.dispatch", QuotaPolicy())
            assert result.allowed is True
            assert session.add.called

    @pytest.mark.asyncio
    async def test_check_at_limit_rejected(self) -> None:
        from investigation_agent_platform.domain.common.quotas import QuotaPolicy
        from investigation_agent_platform.infrastructure.persistence.quota_enforcer import (
            PostgresQuotaEnforcer,
        )

        row = MagicMock()
        row.count = 5
        session = _mock_session(scalars_first=row)
        async with _mock_rls(
            "investigation_agent_platform.infrastructure.persistence.quota_enforcer",
            session,
        ):
            enforcer = PostgresQuotaEnforcer(db_session_factory=MagicMock())
            result = await enforcer.check(
                _scope(), "job.dispatch", QuotaPolicy(max_concurrent_jobs=5)
            )
            assert result.allowed is False
            assert result.retry_after_seconds == 60


# ===========================================================================
# Scoped idempotency
# ===========================================================================


class TestScopedIdempotencyKeys:
    def test_bare_key_backward_compatible(self) -> None:
        from investigation_agent_platform.domain.common.idempotency import (
            scoped_idempotency_key,
        )

        assert scoped_idempotency_key("abc") == "abc"

    def test_scoped_mapping(self) -> None:
        from investigation_agent_platform.domain.common.idempotency import (
            scoped_idempotency_key,
        )

        assert scoped_idempotency_key("abc", "batch", "app-1") == "batch:app-1:abc"
        assert scoped_idempotency_key("abc", "batch") == "batch:-:abc"

    def test_empty_key_rejected(self) -> None:
        from investigation_agent_platform.domain.common.idempotency import (
            scoped_idempotency_key,
        )

        with pytest.raises(ValueError):
            scoped_idempotency_key("")

    @pytest.mark.asyncio
    async def test_operations_do_not_collide(self) -> None:
        from investigation_agent_platform.api.dependencies import _InMemoryIdempotencyStore

        store = _InMemoryIdempotencyStore()
        first, reserved_first = await store.reserve_or_get(
            "tenant-a", "key-1", "hash", operation="batch"
        )
        assert reserved_first is True
        assert first is None
        second, reserved_second = await store.reserve_or_get(
            "tenant-a", "key-1", "hash", operation="jobs"
        )
        assert reserved_second is True

    @pytest.mark.asyncio
    async def test_same_operation_conflict_preserved(self) -> None:
        from investigation_agent_platform.api.dependencies import _InMemoryIdempotencyStore
        from investigation_agent_platform.domain.common.exceptions import (
            IdempotencyConflictError,
        )

        store = _InMemoryIdempotencyStore()
        await store.reserve_or_get("tenant-a", "key-1", "hash-a", operation="batch")
        with pytest.raises(IdempotencyConflictError):
            await store.reserve_or_get("tenant-a", "key-1", "hash-b", operation="batch")


# ===========================================================================
# Signed cursors
# ===========================================================================


class TestPageCursors:
    def test_round_trip(self) -> None:
        from investigation_agent_platform.domain.common.pagination import (
            decode_cursor,
            encode_cursor,
        )

        token = encode_cursor({"sort": "created_at", "tiebreaker": "abc"}, "secret")
        assert decode_cursor(token, "secret") == {
            "sort": "created_at",
            "tiebreaker": "abc",
        }

    def test_tamper_rejected(self) -> None:
        from investigation_agent_platform.domain.common.pagination import (
            CursorError,
            decode_cursor,
            encode_cursor,
        )

        token = encode_cursor({"sort": "x"}, "secret")
        with pytest.raises(CursorError):
            decode_cursor(token[:-2] + ("AA" if not token.endswith("AA") else "BB"), "secret")
        with pytest.raises(CursorError):
            decode_cursor(token, "wrong-secret")

    def test_malformed_rejected(self) -> None:
        from investigation_agent_platform.domain.common.pagination import (
            CursorError,
            decode_cursor,
        )

        for bad in ("", "not-a-cursor", "!!!"):
            with pytest.raises(CursorError):
                decode_cursor(bad, "secret")

    def test_empty_secret_rejected(self) -> None:
        from investigation_agent_platform.domain.common.pagination import (
            CursorError,
            decode_cursor,
            encode_cursor,
        )

        with pytest.raises(CursorError):
            encode_cursor({"a": 1}, "")
        with pytest.raises(CursorError):
            decode_cursor("whatever", "")

    def test_page_cursor_model_defaults(self) -> None:
        from investigation_agent_platform.domain.common.pagination import PageCursor

        cursor = PageCursor()
        assert cursor.sort == "created_at"
        assert cursor.direction == "asc"


# ===========================================================================
# Provenance + lifecycle + audit
# ===========================================================================


class TestProvenance:
    def test_attach_extract_round_trip(self) -> None:
        from investigation_agent_platform.domain.common.provenance import (
            ProvenanceRecord,
            attach_provenance,
            extract_provenance,
        )

        record = ProvenanceRecord(
            plugin_id="git-fetcher",
            plugin_version="1.0",
            input_digests={"repo": "abc123"},
        )
        payload = attach_provenance({"chunks": 3}, record)
        assert payload["chunks"] == 3
        back = extract_provenance(payload)
        assert back is not None
        assert back.plugin_id == "git-fetcher"
        assert back.input_digests == {"repo": "abc123"}

    def test_extract_missing_or_invalid(self) -> None:
        from investigation_agent_platform.domain.common.provenance import extract_provenance

        assert extract_provenance({}) is None
        assert extract_provenance({"_provenance": "nope"}) is None
        assert extract_provenance({"_provenance": {"plugin_id": 1}}) is None


class TestLifecycleAudit:
    def test_lifecycle_values(self) -> None:
        from investigation_agent_platform.domain.common.lifecycle import ResourceLifecycle

        assert {s.value for s in ResourceLifecycle} == {
            "ACTIVE",
            "DISABLED",
            "DELETING",
            "DELETED",
            "FAILED",
        }

    def test_retention_policy_bounds(self) -> None:
        from investigation_agent_platform.domain.common.lifecycle import RetentionPolicy

        policy = RetentionPolicy(retention_class="reports")
        assert policy.retain_days == 90
        assert policy.soft_delete is True

    def test_audit_event_required_fields(self) -> None:
        from investigation_agent_platform.domain.common.lifecycle import AuditEvent

        event = AuditEvent(
            tenant_id="tenant-a",
            actor="tester",
            action="artifact.get",
            resource_type="artifact",
        )
        assert event.outcome == "success"
        assert event.event_id is not None


# ===========================================================================
# Jobs API
# ===========================================================================


class TestJobsApi:
    def _client(self, ctx=None):  # type: ignore[no-untyped-def]
        ctx = ctx or AppContext()
        set_app_context(ctx)
        return TestClient(create_app(), raise_server_exceptions=False), ctx

    def test_get_unknown_404(self, _restore_context: None) -> None:
        client, _ = self._client()
        resp = client.get(f"/api/v1/jobs/{uuid4()}", headers={"X-Tenant-ID": "tenant-a"})
        assert resp.status_code == 404

    def test_get_invalid_uuid_400(self, _restore_context: None) -> None:
        client, _ = self._client()
        resp = client.get("/api/v1/jobs/nope", headers={"X-Tenant-ID": "tenant-a"})
        assert resp.status_code == 400

    def test_requires_tenant(self, _restore_context: None) -> None:
        client, _ = self._client()
        assert client.get("/api/v1/jobs").status_code == 401

    @pytest.mark.asyncio
    async def test_list_envelope_and_kind_filter(self, _restore_context: None) -> None:
        client, ctx = self._client()
        await ctx.background_job_repo.create(_job(kind="alpha"))
        await ctx.background_job_repo.create(_job(kind="beta"))
        resp = client.get("/api/v1/jobs", headers={"X-Tenant-ID": "tenant-a"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 2
        filtered = client.get("/api/v1/jobs?kind=alpha", headers={"X-Tenant-ID": "tenant-a"})
        assert filtered.json()["total"] == 1

    @pytest.mark.asyncio
    async def test_cancel_transitions_and_idempotent(self, _restore_context: None) -> None:
        client, ctx = self._client()
        job = _job(status=BackgroundJobStatus.RUNNING)
        await ctx.background_job_repo.create(job)
        resp = client.post(f"/api/v1/jobs/{job.id}/cancel", headers={"X-Tenant-ID": "tenant-a"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["cancelled"] is True
        assert body["job"]["status"] == "CANCEL_REQUESTED"
        stored = await ctx.background_job_repo.get_by_id("tenant-a", job.id)
        assert stored is not None
        assert stored.status == BackgroundJobStatus.CANCEL_REQUESTED
        # Terminal cancel is a no-op returning current state.
        done = _job(kind="done-kind", status=BackgroundJobStatus.DONE)
        await ctx.background_job_repo.create(done)
        resp2 = client.post(f"/api/v1/jobs/{done.id}/cancel", headers={"X-Tenant-ID": "tenant-a"})
        assert resp2.json()["cancelled"] is False
        assert resp2.json()["job"]["status"] == "DONE"

    @pytest.mark.asyncio
    async def test_cancel_cross_tenant_404(self, _restore_context: None) -> None:
        client, ctx = self._client()
        job = _job()
        await ctx.background_job_repo.create(job)
        resp = client.post(f"/api/v1/jobs/{job.id}/cancel", headers={"X-Tenant-ID": "tenant-b"})
        assert resp.status_code == 404


# ===========================================================================
# WebSocket streaming
# ===========================================================================


class TestJobStream:
    def _client(self, ctx):  # type: ignore[no-untyped-def]
        set_app_context(ctx)
        return TestClient(create_app(), raise_server_exceptions=False)

    @pytest.mark.asyncio
    async def test_snapshot_then_terminal_event_then_close(self, _restore_context: None) -> None:
        import asyncio as _asyncio

        ctx = AppContext()
        client = self._client(ctx)
        job = _job(status=BackgroundJobStatus.RUNNING)
        await ctx.background_job_repo.create(job)

        def _publish_done() -> None:
            time.sleep(0.5)
            _asyncio.run(ctx.job_hub.publish(_event(job.id, BackgroundJobStatus.DONE)))

        thread = threading.Thread(target=_publish_done, daemon=True)
        with client.websocket_connect(
            f"/api/v1/jobs/{job.id}/stream", headers={"X-Tenant-ID": "tenant-a"}
        ) as ws:
            thread.start()
            snapshot = ws.receive_json()
            assert snapshot["type"] == "snapshot"
            assert snapshot["job"]["id"] == str(job.id)
            message = ws.receive_json()
            assert message["type"] == "event"
            assert message["event"]["status"] == "DONE"
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        thread.join(timeout=10)

    @pytest.mark.asyncio
    async def test_terminal_job_snapshot_then_close(self, _restore_context: None) -> None:
        ctx = AppContext()
        client = self._client(ctx)
        job = _job(status=BackgroundJobStatus.DONE)
        await ctx.background_job_repo.create(job)
        with client.websocket_connect(
            f"/api/v1/jobs/{job.id}/stream", headers={"X-Tenant-ID": "tenant-a"}
        ) as ws:
            snapshot = ws.receive_json()
            assert snapshot["type"] == "snapshot"
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()

    def test_unknown_job_closes_4404(self, _restore_context: None) -> None:
        ctx = AppContext()
        client = self._client(ctx)
        with client.websocket_connect(
            f"/api/v1/jobs/{uuid4()}/stream", headers={"X-Tenant-ID": "tenant-a"}
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 4404

    def test_missing_tenant_rejected(self, _restore_context: None) -> None:
        from fastapi.websockets import WebSocketDisconnect

        ctx = AppContext()
        client = self._client(ctx)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(f"/api/v1/jobs/{uuid4()}/stream"):
                pass
        assert exc_info.value.code == 4401


# ===========================================================================
# Hub fan-out
# ===========================================================================


class TestJobHub:
    @pytest.mark.asyncio
    async def test_subscribe_publish_unsubscribe(self) -> None:
        from investigation_agent_platform.infrastructure.messaging.job_fanout import (
            JobProgressHub,
        )

        hub = JobProgressHub()
        job_id = uuid4()
        assert await hub.publish(_event(job_id)) is False
        queue = await hub.subscribe(job_id)
        assert await hub.publish(_event(job_id)) is True
        import asyncio as _asyncio

        received = await _asyncio.wait_for(queue.get(), timeout=5)
        assert received.job_id == job_id
        await hub.unsubscribe(job_id, queue)
        assert await hub.publish(_event(job_id)) is False

    @pytest.mark.asyncio
    async def test_duplicate_event_suppressed(self) -> None:
        import asyncio as _asyncio

        from investigation_agent_platform.infrastructure.messaging.job_fanout import (
            JobProgressHub,
        )

        hub = JobProgressHub()
        job_id = uuid4()
        queue = await hub.subscribe(job_id)
        event = _event(job_id)
        assert await hub.publish(event) is True
        assert await hub.publish(event) is True  # subscriber exists, already seen
        first = await _asyncio.wait_for(queue.get(), timeout=5)
        assert first.event_id == event.event_id
        with pytest.raises(_asyncio.TimeoutError):
            await _asyncio.wait_for(queue.get(), timeout=0.2)

    @pytest.mark.asyncio
    async def test_multi_subscriber_fanout(self) -> None:
        import asyncio as _asyncio

        from investigation_agent_platform.infrastructure.messaging.job_fanout import (
            JobProgressHub,
        )

        hub = JobProgressHub()
        job_id = uuid4()
        first = await hub.subscribe(job_id)
        second = await hub.subscribe(job_id)
        await hub.publish(_event(job_id))
        assert (await _asyncio.wait_for(first.get(), timeout=5)).job_id == job_id
        assert (await _asyncio.wait_for(second.get(), timeout=5)).job_id == job_id

    def test_replica_groups_unique_per_replica(self) -> None:
        from investigation_agent_platform.infrastructure.messaging.job_fanout import (
            replica_consumer_group,
            resolve_replica_id,
        )

        assert replica_consumer_group("jobs", "r1") == "jobs-r1"
        assert replica_consumer_group("jobs", "r1") != replica_consumer_group("jobs", "r2")
        assert resolve_replica_id()

    def test_job_topic_convention(self) -> None:
        from investigation_agent_platform.ports.messaging.publisher import job_topic_for

        assert job_topic_for("tenant-a", "iap") == "iap-jobs.tenant-a"


# ===========================================================================
# Kafka consumer wiring (in-memory broker)
# ===========================================================================


class TestKafkaConsumerWiring:
    @pytest.mark.asyncio
    async def test_consumer_delivers_event_to_hub(self) -> None:
        from faststream.kafka import KafkaBroker, TestKafkaBroker

        from investigation_agent_platform.infrastructure.messaging.job_fanout import (
            JobProgressHub,
            build_job_event_consumer,
        )

        broker = KafkaBroker("localhost:9092")
        hub = JobProgressHub()
        build_job_event_consumer(broker, hub, ["jobs.t"], "jobs-test-group")
        job_id = uuid4()
        event = _event(job_id)
        queue = await hub.subscribe(job_id)
        async with TestKafkaBroker(broker) as test_broker:
            await test_broker.publish(event.model_dump(mode="json"), topic="jobs.t")
            import asyncio as _asyncio

            received = await _asyncio.wait_for(queue.get(), timeout=10)
            assert received.job_id == job_id

    @pytest.mark.asyncio
    async def test_malformed_payload_skipped(self) -> None:
        from faststream.kafka import KafkaBroker, TestKafkaBroker

        from investigation_agent_platform.infrastructure.messaging.job_fanout import (
            JobProgressHub,
            build_job_event_consumer,
        )

        broker = KafkaBroker("localhost:9092")
        hub = JobProgressHub()
        build_job_event_consumer(broker, hub, ["jobs.m"], "jobs-malformed-group")
        job_id = uuid4()
        queue = await hub.subscribe(job_id)
        async with TestKafkaBroker(broker) as test_broker:
            await test_broker.publish({"not": "an-event"}, topic="jobs.m")
            await test_broker.publish(_event(job_id).model_dump(mode="json"), topic="jobs.m")
            import asyncio as _asyncio

            received = await _asyncio.wait_for(queue.get(), timeout=10)
            assert received.job_id == job_id


# ===========================================================================
# Schedules + history policy
# ===========================================================================


class TestSchedules:
    def test_descriptor_defaults(self) -> None:
        from investigation_agent_platform.domain.common.schedules import ScheduleDescriptor

        descriptor = ScheduleDescriptor(
            schedule_id="sched-1",
            workflow_type="FindingClusteringWorkflow",
            task_queue="analytics-tasks",
        )
        assert descriptor.overlap_policy.value == "SKIP"
        assert descriptor.paused is False

    def test_history_policy_predicates(self) -> None:
        from investigation_agent_platform.domain.common.schedules import WorkflowHistoryPolicy

        policy = WorkflowHistoryPolicy()
        assert not policy.should_warn(100)
        assert policy.should_warn(8_000)
        assert not policy.should_roll_over(9_999)
        assert policy.should_roll_over(10_000)

    @pytest.mark.asyncio
    async def test_reconciler_creates_then_updates(self) -> None:
        from investigation_agent_platform.domain.common.schedules import ScheduleDescriptor
        from investigation_agent_platform.infrastructure.scheduling.reconciler import (
            TemporalScheduleReconciler,
        )

        created: list = []
        updated: list = []

        class _Handle:
            async def describe(self) -> None:
                raise RuntimeError("not found")

            async def update(self, schedule) -> None:  # type: ignore[no-untyped-def]
                updated.append(schedule)

            async def pause(self) -> None:
                pass

            async def unpause(self) -> None:
                pass

        class _Client:
            def get_schedule_handle(self, schedule_id):  # type: ignore[no-untyped-def]
                return _Handle()

            async def create_schedule(self, schedule_id, schedule) -> None:  # type: ignore[no-untyped-def]
                created.append(schedule_id)

        reconciler = TemporalScheduleReconciler(_Client(), workflow_resolver={"Wf": "Wf"})
        descriptor = ScheduleDescriptor(
            schedule_id="s1", workflow_type="Wf", task_queue="analytics-tasks"
        )
        assert await reconciler.reconcile([descriptor]) == ["s1"]
        assert created == ["s1"]

    @pytest.mark.asyncio
    async def test_reconciler_collects_unknown_workflow(self) -> None:
        from investigation_agent_platform.domain.common.schedules import ScheduleDescriptor
        from investigation_agent_platform.infrastructure.scheduling.reconciler import (
            TemporalScheduleReconciler,
        )

        reconciler = TemporalScheduleReconciler(MagicMock(), workflow_resolver={})
        descriptor = ScheduleDescriptor(
            schedule_id="bad", workflow_type="Missing", task_queue="analytics-tasks"
        )
        assert await reconciler.reconcile([descriptor]) == []

    @pytest.mark.asyncio
    async def test_active_schedule_ids(self) -> None:
        from investigation_agent_platform.infrastructure.scheduling.reconciler import (
            TemporalScheduleReconciler,
        )

        class _Entry:
            id = "sched-9"

        class _AsyncList:
            def __init__(self, items):  # type: ignore[no-untyped-def]
                self._items = items

            def __aiter__(self):  # type: ignore[no-untyped-def]
                return self._gen()

            async def _gen(self):  # type: ignore[no-untyped-def]
                for item in self._items:
                    yield item

        class _Client:
            async def list_schedules(self):  # type: ignore[no-untyped-def]
                return _AsyncList([_Entry()])

        reconciler = TemporalScheduleReconciler(_Client(), workflow_resolver={})
        assert await reconciler.active_schedule_ids() == ["sched-9"]


# ===========================================================================
# Telemetry helpers
# ===========================================================================


class TestJobTelemetry:
    def test_transition_emits_bounded_tags(self) -> None:
        from investigation_agent_platform.infrastructure.observability.job_telemetry import (
            record_job_transition,
        )

        observability = MagicMock()
        record_job_transition(observability, _job(), "QUEUED")
        observability.record_metric.assert_called_once()
        name, value, tags = observability.record_metric.call_args.args
        assert name == "job_transition"
        assert set(tags) == {"kind", "status"}
        assert "tenant-a" not in str(tags)

    def test_none_observability_noop(self) -> None:
        from investigation_agent_platform.infrastructure.observability.job_telemetry import (
            record_job_progress,
            record_quota_decision,
            record_stream_event,
        )

        record_job_progress(None, _event(uuid4()))
        record_quota_decision(None, "job.dispatch", True, "tenant-a")
        record_stream_event(None, "open", "tenant-a", "job-1")

    def test_quota_rejection_logged(self) -> None:
        from investigation_agent_platform.infrastructure.observability.job_telemetry import (
            record_quota_decision,
        )

        observability = MagicMock()
        record_quota_decision(observability, "job.dispatch", False, "tenant-a")
        observability.record_metric.assert_called_once()
        assert observability.record_metric.call_args.args[2]["status"] == "rejected"


# ===========================================================================
# Retention / purge contract
# ===========================================================================


class TestRetentionPurge:
    @pytest.mark.asyncio
    async def test_purge_kind_lists_by_retention_class(self) -> None:
        repo = InMemoryBackgroundJobRepository()
        job = _job(kind="purge:artifacts")
        await repo.create(job)
        items, total = await repo.list_jobs("tenant-a", kind="purge:artifacts")
        assert total == 1
        assert items[0].retention_class == "default"

    def test_retention_policy_model(self) -> None:
        from investigation_agent_platform.domain.common.lifecycle import (
            ResourceLifecycle,
            RetentionPolicy,
        )

        policy = RetentionPolicy(
            retention_class="jobs",
            retain_days=30,
            soft_delete=True,
            legal_hold_supported=True,
            purge_job_kind="purge:jobs",
        )
        assert policy.purge_job_kind == "purge:jobs"
        assert ResourceLifecycle.DELETING.value == "DELETING"


# ===========================================================================
# Migration 010
# ===========================================================================


class TestMigration010:
    def test_010_chain(self) -> None:
        import importlib.util
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[2]
            / "migrations"
            / "versions"
            / "010_background_jobs_and_quotas.py"
        )
        assert path.is_file()
        spec = importlib.util.spec_from_file_location("migration_010", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.revision == "010_background_jobs_and_quotas"
        assert mod.down_revision == "009_profile_revisions"

    def test_010_remains_ancestor(self) -> None:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config("alembic.ini")
        script = ScriptDirectory.from_config(cfg)
        rev = script.get_revision("010_background_jobs_and_quotas")
        assert rev is not None
        assert rev.down_revision == "009_profile_revisions"
        chain: list[str] = []
        current = script.get_revision(script.get_heads()[0])
        while current is not None:
            chain.append(current.revision)
            down = current.down_revision
            current = script.get_revision(down) if down else None
        assert "010_background_jobs_and_quotas" in chain


# ===========================================================================
# Live Postgres (opt-in, skips when unreachable)
# ===========================================================================


def _pg_reachable() -> bool:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(PG_URL)
        with socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or 5432), timeout=1.0
        ):
            return True
    except OSError:
        return False


@requires_containers
class TestLivePostgresJobs:
    @pytest.mark.asyncio
    async def test_jobs_crud_and_rls(self) -> None:
        if not _pg_reachable():
            pytest.skip("Postgres unreachable")
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from investigation_agent_platform.infrastructure.persistence.background_job_repository import (
            SqlAlchemyBackgroundJobRepository,
        )
        from investigation_agent_platform.infrastructure.persistence.models import (
            BackgroundJobORM,
            Base,
            QuotaCounterORM,
        )

        url = PG_URL.replace("postgresql://", "postgresql+asyncpg://")
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(
                    Base.metadata.create_all,
                    tables=[BackgroundJobORM.__table__, QuotaCounterORM.__table__],
                    checkfirst=True,
                )
                await conn.execute(
                    __import__("sqlalchemy").text(
                        "DO $$ BEGIN "
                        "IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename="
                        "'background_jobs' AND policyname='bg_jobs_tenant') THEN "
                        "CREATE POLICY bg_jobs_tenant ON background_jobs USING "
                        "(tenant_id = current_setting('app.tenant_id', true)) WITH CHECK "
                        "(tenant_id = current_setting('app.tenant_id', true)); "
                        "END IF; END $$;"
                    )
                )
                await conn.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE background_jobs ENABLE ROW LEVEL SECURITY"
                    )
                )
            factory = async_sessionmaker(engine, expire_on_commit=False)
            repo = SqlAlchemyBackgroundJobRepository(db_session_factory=factory)
            job = _job()
            await repo.create(job)
            got = await repo.get_by_id("tenant-a", job.id)
            assert got is not None
            assert got.kind == "test-kind"
            assert await repo.get_by_id("tenant-b", job.id) is None
            updated = got.model_copy(update={"progress": 2, "version": 2})
            await repo.save("tenant-a", updated, expected_version=1)
            items, total = await repo.list_jobs("tenant-a")
            assert total >= 1
            assert any(i.id == job.id for i in items)
        finally:
            await engine.dispose()
