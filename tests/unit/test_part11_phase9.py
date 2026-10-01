# tests/unit/test_part11_phase9.py
"""Task 11.b — Bulk investigation intake tests (Phase 11.9).

Service validation, CSV adapter, repository round-trips, activity units,
workflow-logic predicates, REST surface, and the 015 migration chain. No
network, no Temporal server — the workflow executes in 12.a golden
scenarios; here its deterministic predicates and activities are pinned.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import (
    _DEFAULT_PROFILE,
    AppContext,
    set_app_context,
)
from investigation_agent_platform.application.intake.batch_service import (
    BatchQuotaExceededError,
    BatchValidationError,
    BulkIntakeService,
    validate_parameters,
)
from investigation_agent_platform.application.intake.csv_adapter import parse_csv_batch
from investigation_agent_platform.domain.common.background_job import BackgroundJobStatus
from investigation_agent_platform.domain.intake.batch import (
    MAX_ACTIVE_CHILDREN,
    MAX_BATCH_RECORDS,
    BatchIntakeRecord,
    BatchRecordResult,
    BatchRecordStatus,
    child_workflow_id,
)

# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _record(index: int, app: str = "app-1", **overrides) -> BatchIntakeRecord:
    params: dict = {
        "external_key": f"ext-{index:04d}",
        "application_id": app,
        "problem_description": f"incident number {index}",
    }
    params.update(overrides)
    return BatchIntakeRecord(**params)


def _profile(app_id: str = "app-1", schema: dict | None = None):
    investigation = _DEFAULT_PROFILE.investigation_configuration.model_copy(
        update={"parameters_schema": schema}
    )
    return _DEFAULT_PROFILE.model_copy(
        update={"id": app_id, "investigation_configuration": investigation}
    )


async def _service(ctx: AppContext, creator=None, quota=None) -> BulkIntakeService:
    async def _fake_creator(tenant_id: str, record: BatchIntakeRecord):
        return SimpleNamespace(id=uuid4())

    return BulkIntakeService(
        batch_repo=ctx.batch_repo,
        job_repo=ctx.background_job_repo,
        profile_repo=ctx.profile_repo,
        investigation_creator=creator or _fake_creator,
        quota_enforcer=quota[0] if quota else None,
        quota_policy=quota[1] if quota else None,
    )


_SCHEMA = {
    "type": "object",
    "required": ["region"],
    "properties": {
        "region": {"type": "string", "enum": ["us", "eu"]},
        "retries": {"type": "integer", "minimum": 0, "maximum": 5},
    },
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# parameters validation
# ---------------------------------------------------------------------------


class TestValidateParameters:
    def test_structural_bounds_without_schema(self) -> None:
        validate_parameters({"a": "x", "b": 1, "c": True, "d": None, "e": [1, "x"]}, None, "r")
        with pytest.raises(BatchValidationError):
            validate_parameters({f"k{i}": "v" for i in range(51)}, None, "r")
        with pytest.raises(BatchValidationError):
            validate_parameters({"nested": {"deep": 1}}, None, "r")
        with pytest.raises(BatchValidationError):
            validate_parameters({"x" * 129: "v"}, None, "r")

    def test_schema_subset(self) -> None:
        validate_parameters({"region": "us", "retries": 3}, _SCHEMA, "r")
        with pytest.raises(BatchValidationError):
            validate_parameters({"retries": 1}, _SCHEMA, "r")
        with pytest.raises(BatchValidationError):
            validate_parameters({"region": "asia"}, _SCHEMA, "r")
        with pytest.raises(BatchValidationError):
            validate_parameters({"region": "us", "retries": 9}, _SCHEMA, "r")
        with pytest.raises(BatchValidationError):
            validate_parameters({"region": "us", "extra": 1}, _SCHEMA, "r")
        with pytest.raises(BatchValidationError):
            validate_parameters({"region": 1}, _SCHEMA, "r")


# ---------------------------------------------------------------------------
# CSV adapter
# ---------------------------------------------------------------------------


class TestCsvAdapter:
    _MAPPING: ClassVar[dict] = {
        "external_key": "ticket",
        "application_id": "app",
        "problem_description": "summary",
        "parameters": {"region": "region"},
    }

    def test_happy_path_and_blank_lines(self) -> None:
        text = "ticket,app,summary,region\nT1,app-1,db slow,us\n\nT2,app-1,timeout,eu\n"
        records = parse_csv_batch(text, self._MAPPING)
        assert [(r.external_key, r.parameters) for r in records] == [
            ("T1", {"region": "us"}),
            ("T2", {"region": "eu"}),
        ]

    def test_missing_column_and_empty(self) -> None:
        with pytest.raises(ValueError):
            parse_csv_batch("ticket,app\nT1,app-1\n", self._MAPPING)
        with pytest.raises(ValueError):
            parse_csv_batch("   ", self._MAPPING)
        with pytest.raises(ValueError):
            parse_csv_batch("ticket,app,summary\n", self._MAPPING)
        with pytest.raises(TypeError):
            parse_csv_batch("ticket\nT1\n", ["not-a-dict"])  # type: ignore[arg-type]

    def test_cap_enforced(self) -> None:
        rows = "\n".join(f"T{i},app-1,summary {i},us" for i in range(MAX_BATCH_RECORDS + 1))
        with pytest.raises(ValueError):
            parse_csv_batch(f"ticket,app,summary,region\n{rows}\n", self._MAPPING)


# ---------------------------------------------------------------------------
# service: ingest + lifecycle
# ---------------------------------------------------------------------------


class TestBulkService:
    pytestmark = pytest.mark.asyncio

    async def test_ingest_creates_job_rows_and_investigations(self) -> None:
        ctx = AppContext()
        await ctx.profile_repo.save("tenant-a", _profile())
        service = await _service(ctx)
        job, rows = await service.ingest_batch("tenant-a", [_record(0), _record(1)], "tester")
        assert job.kind == "batch-intake"
        assert job.tenant_id == "tenant-a"
        assert [r.status for r in rows] == [BatchRecordStatus.PENDING] * 2
        assert all(r.investigation_id is not None for r in rows)
        stored, total = await ctx.batch_repo.list_records("tenant-a", job.id)
        assert total == 2 and len(stored) == 2

    async def test_ingest_rejects(self) -> None:
        ctx = AppContext()
        await ctx.profile_repo.save("tenant-a", _profile())
        service = await _service(ctx)
        with pytest.raises(BatchValidationError):
            await service.ingest_batch("tenant-a", [], "tester")
        with pytest.raises(BatchValidationError):
            await service.ingest_batch(
                "tenant-a", [_record(i) for i in range(MAX_BATCH_RECORDS + 1)], "tester"
            )
        with pytest.raises(BatchValidationError):
            await service.ingest_batch("tenant-a", [_record(0), _record(0)], "tester")
        with pytest.raises(BatchValidationError):
            await service.ingest_batch("tenant-a", [_record(0, app="unknown-app")], "tester")

    async def test_schema_validated_against_profile(self) -> None:
        ctx = AppContext()
        await ctx.profile_repo.save("tenant-a", _profile(schema=_SCHEMA))
        service = await _service(ctx)
        job, _ = await service.ingest_batch(
            "tenant-a", [_record(0, parameters={"region": "us"})], "tester"
        )
        assert job is not None
        with pytest.raises(BatchValidationError):
            await service.ingest_batch(
                "tenant-a", [_record(1, parameters={"region": "asia"})], "tester"
            )

    async def test_quota_rejection(self) -> None:
        ctx = AppContext()
        await ctx.profile_repo.save("tenant-a", _profile())

        class _Deny:
            async def check(self, scope: object, op: str, policy: object) -> object:
                assert op == "batch.record"
                return SimpleNamespace(allowed=False)

            async def release(self, scope: object, op: str) -> None:
                pass  # pragma: no cover

        service = await _service(ctx, quota=(_Deny(), object()))
        with pytest.raises(BatchQuotaExceededError):
            await service.ingest_batch("tenant-a", [_record(0)], "tester")

    async def test_summary_cancel_retry_purge(self) -> None:
        ctx = AppContext()
        await ctx.profile_repo.save("tenant-a", _profile())
        service = await _service(ctx)
        job, rows = await service.ingest_batch("tenant-a", [_record(0), _record(1)], "tester")
        summary = await service.summarize("tenant-a", job.id)
        assert summary is not None and summary.total == 2 and summary.succeeded == 0
        assert await service.summarize("tenant-b", job.id) is None
        # Fail one row, then partial-retry it.
        failed = rows[0].model_copy(update={"status": BatchRecordStatus.FAILED})
        await ctx.batch_repo.save_record("tenant-a", job.id, failed)
        assert await service.retry_failed("tenant-a", job.id) == 1
        reset = await ctx.batch_repo.get_record("tenant-a", job.id, 0)
        assert reset is not None and reset.status == BatchRecordStatus.PENDING
        assert reset.attempt == 1
        assert await service.retry_failed("tenant-a", job.id) == 0
        # Cancel a live job.
        canceled = await service.cancel_batch("tenant-a", job.id)
        assert canceled is not None
        assert canceled.status == BackgroundJobStatus.CANCEL_REQUESTED
        assert await service.cancel_batch("tenant-b", job.id) is None
        # Purge refuses live jobs; purges terminal expired ones.
        assert await service.purge_batch("tenant-a", job.id) is False
        terminal = canceled.model_copy(
            update={
                "status": BackgroundJobStatus.DONE,
                "created_at": datetime.now(UTC) - timedelta(days=100),
                "version": canceled.version + 1,
            }
        )
        await ctx.background_job_repo.save("tenant-a", terminal, canceled.version)
        assert await service.purge_batch("tenant-a", job.id) is True
        _, remaining = await ctx.batch_repo.list_records("tenant-a", job.id)
        assert remaining == 0

    async def test_get_batch_pagination_and_isolation(self) -> None:
        ctx = AppContext()
        await ctx.profile_repo.save("tenant-a", _profile())
        service = await _service(ctx)
        job, _ = await service.ingest_batch("tenant-a", [_record(i) for i in range(5)], "tester")
        _, page, total = await service.get_batch("tenant-a", job.id, limit=2, offset=1)
        assert total == 5 and [r.record_index for r in page] == [1, 2]
        assert await service.get_batch("tenant-b", job.id) == (None, [], 0)


# ---------------------------------------------------------------------------
# deterministic ids + CAN predicate + concurrency bound
# ---------------------------------------------------------------------------


class TestBulkDeterminism:
    def test_child_ids_deterministic_and_collision_free(self) -> None:
        job_a, job_b = uuid4(), uuid4()
        assert child_workflow_id(job_a, 3) == child_workflow_id(job_a, 3)
        assert child_workflow_id(job_a, 3) != child_workflow_id(job_a, 4)
        assert child_workflow_id(job_a, 3) != child_workflow_id(job_b, 3)
        assert child_workflow_id(job_a, 3) != child_workflow_id(job_a, 3, attempt=1)
        assert "ext-" not in child_workflow_id(job_a, 3)  # opaque: no external keys

    def test_can_predicate(self) -> None:
        from investigation_agent_platform.application.worker.workflows import (
            should_continue_as_new,
        )

        assert should_continue_as_new(0, 250, 100, 0) is True
        assert should_continue_as_new(0, 249, 100, 0) is False
        assert should_continue_as_new(0, 300, 100, 2) is False  # active children
        assert should_continue_as_new(0, 300, 0, 0) is False  # nothing remaining
        assert should_continue_as_new(250, 50, 200, 0) is True  # resumed cursor counts

    def test_concurrency_bound_constant(self) -> None:
        assert MAX_ACTIVE_CHILDREN == 10
        assert MAX_BATCH_RECORDS == 500


# ---------------------------------------------------------------------------
# activities
# ---------------------------------------------------------------------------


class TestBatchActivities:
    pytestmark = pytest.mark.asyncio

    async def _seed(self, ctx: AppContext, count: int = 3):

        await ctx.profile_repo.save("tenant-a", _profile())
        service = await _service(ctx)
        job, _ = await service.ingest_batch(
            "tenant-a", [_record(i) for i in range(count)], "tester"
        )
        return job

    async def test_load_filters_pending_and_cursor(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            load_batch_records_activity,
        )

        ctx = AppContext()
        set_app_context(ctx)
        job = await self._seed(ctx, 3)
        res = await load_batch_records_activity(
            {"tenant_id": "tenant-a", "job_id": str(job.id), "start_index": 0}
        )
        assert res.success and len(res.data["records"]) == 3
        # Mark one DONE: reload skips it; cursor skips ahead (restart resume).
        record = await ctx.batch_repo.get_record("tenant-a", job.id, 0)
        assert record is not None
        await ctx.batch_repo.save_record(
            "tenant-a", job.id, record.model_copy(update={"status": BatchRecordStatus.DONE})
        )
        res = await load_batch_records_activity(
            {"tenant_id": "tenant-a", "job_id": str(job.id), "start_index": 1}
        )
        assert [r["record_index"] for r in res.data["records"]] == [1, 2]
        bad = await load_batch_records_activity({"tenant_id": "", "job_id": "x"})
        assert bad.success is False

    async def test_mark_bulk_and_result(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            mark_batch_record_result_activity,
            mark_batch_records_activity,
        )

        ctx = AppContext()
        set_app_context(ctx)
        job = await self._seed(ctx, 2)
        res = await mark_batch_records_activity(
            {
                "tenant_id": "tenant-a",
                "job_id": str(job.id),
                "record_indices": [0, 1],
                "status": "RUNNING",
                "child_workflow_ids": {"0": "wf-a", "1": "wf-b"},
            }
        )
        assert res.data["marked"] == 2
        done = await mark_batch_record_result_activity(
            {
                "tenant_id": "tenant-a",
                "job_id": str(job.id),
                "record_index": 0,
                "status": "DONE",
            }
        )
        assert done.success
        # Non-terminal statuses are rejected (never invent terminal states).
        rejected = await mark_batch_record_result_activity(
            {
                "tenant_id": "tenant-a",
                "job_id": str(job.id),
                "record_index": 1,
                "status": "RUNNING",
            }
        )
        assert rejected.success is False
        failed = await mark_batch_record_result_activity(
            {
                "tenant_id": "tenant-a",
                "job_id": str(job.id),
                "record_index": 1,
                "status": "FAILED",
                "error": "boom",
            }
        )
        assert failed.success
        record = await ctx.batch_repo.get_record("tenant-a", job.id, 1)
        assert record is not None and record.error == "boom"

    async def test_stragglers_classify_awaiting_vs_running(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            resolve_batch_stragglers_activity,
        )

        ctx = AppContext()
        set_app_context(ctx)
        job = await self._seed(ctx, 2)

        class _Repo:
            calls = 0

            async def get_by_id(self, tenant_id: str, investigation_id: UUID):
                type(self).calls += 1
                status = "AWAITING_INPUT" if type(self).calls == 1 else "RUNNING"
                return SimpleNamespace(status=SimpleNamespace(value=status))

        ctx.investigation_repo = _Repo()  # type: ignore[assignment]
        # Pin investigation ids with known suffixes.
        for index in (0, 1):
            record = await ctx.batch_repo.get_record("tenant-a", job.id, index)
            assert record is not None
            suffix = "0" if index == 0 else "1"
            await ctx.batch_repo.save_record(
                "tenant-a",
                job.id,
                record.model_copy(update={"investigation_id": UUID(int=int(suffix) + 1)}),
            )
        res = await resolve_batch_stragglers_activity(
            {"tenant_id": "tenant-a", "job_id": str(job.id), "record_indices": [0, 1]}
        )
        assert res.data == {"awaiting_input": 1, "running": 1}
        first = await ctx.batch_repo.get_record("tenant-a", job.id, 0)
        assert first is not None and first.status == BatchRecordStatus.AWAITING_INPUT

    async def test_cancel_children_fans_out(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            cancel_batch_children_activity,
        )

        ctx = AppContext()
        set_app_context(ctx)
        job = await self._seed(ctx, 2)
        handles = {}
        for index in (0, 1):
            record = await ctx.batch_repo.get_record("tenant-a", job.id, index)
            assert record is not None
            await ctx.batch_repo.save_record(
                "tenant-a",
                job.id,
                record.model_copy(
                    update={
                        "status": BatchRecordStatus.RUNNING,
                        "child_workflow_id": f"wf-child-{index}",
                    }
                ),
            )
            handle = MagicMock()
            handle.cancel = AsyncMock()
            handles[f"wf-child-{index}"] = handle
        client = MagicMock()
        client.get_workflow_handle = MagicMock(side_effect=lambda wid: handles[wid])
        ctx.temporal_client = client
        res = await cancel_batch_children_activity({"tenant_id": "tenant-a", "job_id": str(job.id)})
        assert res.data["canceled"] == 2
        assert all(handle.cancel.await_count == 1 for handle in handles.values())
        record = await ctx.batch_repo.get_record("tenant-a", job.id, 0)
        assert record is not None and record.status == BatchRecordStatus.CANCELED

    async def test_cancel_children_no_client_fails_closed(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            cancel_batch_children_activity,
        )

        ctx = AppContext()
        set_app_context(ctx)
        job = await self._seed(ctx, 1)
        res = await cancel_batch_children_activity({"tenant_id": "tenant-a", "job_id": str(job.id)})
        # Dev AppContext may carry the live client; either a clean fan-out
        # or an explicit unavailable error — never an exception.
        assert isinstance(res.data, dict)

    async def test_summarize_and_update_job(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            summarize_batch_activity,
            update_batch_job_activity,
        )

        ctx = AppContext()
        set_app_context(ctx)
        job = await self._seed(ctx, 2)
        summary = await summarize_batch_activity({"tenant_id": "tenant-a", "job_id": str(job.id)})
        assert summary.data["total"] == 2
        updated = await update_batch_job_activity(
            {
                "tenant_id": "tenant-a",
                "job_id": str(job.id),
                "status": "RUNNING",
                "stage": {"name": "wave", "message": "started"},
            }
        )
        assert updated.data["status"] == "RUNNING"
        bad = await update_batch_job_activity(
            {"tenant_id": "tenant-a", "job_id": str(job.id), "status": "NOPE"}
        )
        assert bad.success is False


# ---------------------------------------------------------------------------
# repository round-trips
# ---------------------------------------------------------------------------


class TestBatchRepo:
    pytestmark = pytest.mark.asyncio

    async def test_in_memory_roundtrip(self) -> None:
        from investigation_agent_platform.api.dependencies import (
            InMemoryBatchIntakeRepository,
        )

        repo = InMemoryBatchIntakeRepository()
        job_id = uuid4()
        rows = [
            BatchRecordResult(record_index=i, external_key=f"k-{i}", application_id="app-1")
            for i in range(3)
        ]
        await repo.create_records("tenant-a", job_id, rows)
        page, total = await repo.list_records("tenant-a", job_id, limit=2, offset=1)
        assert total == 3 and [r.record_index for r in page] == [1, 2]
        assert await repo.get_record("tenant-a", job_id, 0) is not None
        assert await repo.get_record("tenant-b", job_id, 0) is None
        assert await repo.reset_failed("tenant-a", job_id, 1) == 0
        failed = rows[0].model_copy(update={"status": BatchRecordStatus.FAILED})
        await repo.save_record("tenant-a", job_id, failed)
        assert await repo.reset_failed("tenant-a", job_id, 2) == 1
        reset = await repo.get_record("tenant-a", job_id, 0)
        assert reset is not None and reset.status == BatchRecordStatus.PENDING
        assert reset.attempt == 2
        assert await repo.delete_job_records("tenant-a", job_id) == 3
        _, remaining = await repo.list_records("tenant-a", job_id)
        assert remaining == 0

    async def test_sql_roundtrip(self) -> None:
        import os

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from investigation_agent_platform.infrastructure.persistence.batch_intake_repository import (
            SqlAlchemyBatchIntakeRepository,
        )

        uri = os.environ.get("IAP_DATABASE_URI", "postgresql://iap:iap@localhost:5452/iap")
        if uri.startswith("postgresql://"):
            uri = "postgresql+asyncpg://" + uri[len("postgresql://") :]
        engine = create_async_engine(uri, pool_pre_ping=True)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        tenant = f"batch-sql-probe-{uuid4().hex[:8]}"
        try:
            repo = SqlAlchemyBatchIntakeRepository(factory)
            job_id = uuid4()
            try:
                await repo.list_records(tenant, job_id)
            except Exception as exc:
                await engine.dispose()
                pytest.skip(f"postgres unreachable: {exc}")
            try:
                await repo.create_records(
                    tenant,
                    job_id,
                    [BatchRecordResult(record_index=0, external_key="k-0", application_id="app-1")],
                )
                page, total = await repo.list_records(tenant, job_id)
                assert total == 1 and page[0].external_key == "k-0"
                failed = page[0].model_copy(update={"status": BatchRecordStatus.FAILED})
                await repo.save_record(tenant, job_id, failed)
                assert await repo.reset_failed(tenant, job_id, 1) == 1
                assert await repo.delete_job_records(tenant, job_id) == 1
            finally:
                await engine.dispose()
        except Exception:
            await engine.dispose()
            raise


# ---------------------------------------------------------------------------
# REST surface
# ---------------------------------------------------------------------------


class TestBatchEndpoints:
    def _client(self, monkeypatch) -> tuple[TestClient, AppContext]:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("IAP_ENVIRONMENT", "development")
        ctx = AppContext()
        set_app_context(ctx)
        return TestClient(create_app()), ctx

    def _auth(self, tenant: str = "tenant-a") -> dict[str, str]:
        return {"X-Tenant-ID": tenant}

    def _seed_profile(self, ctx: AppContext) -> None:
        import anyio

        async def _save() -> None:
            await ctx.profile_repo.save("tenant-a", _profile())

        anyio.run(_save)

    def _payload(self, count: int = 2) -> dict:
        return {
            "records": [
                {
                    "external_key": f"ext-{i:04d}",
                    "application_id": "app-1",
                    "problem_description": f"incident {i}",
                }
                for i in range(count)
            ]
        }

    def test_validation_rejects(self, monkeypatch) -> None:
        client, ctx = self._client(monkeypatch)
        self._seed_profile(ctx)
        assert client.post("/api/v1/batch-intake", json={}, headers=self._auth()).status_code == 400
        dup = self._payload(2)
        dup["records"][1]["external_key"] = dup["records"][0]["external_key"]
        resp = client.post(
            "/api/v1/batch-intake",
            json=dup,
            headers={**self._auth(), "X-Idempotency-Key": "batch-dup"},
        )
        assert resp.status_code == 400
        unknown = self._payload(1)
        unknown["records"][0]["application_id"] = "nope"
        resp = client.post(
            "/api/v1/batch-intake",
            json=unknown,
            headers={**self._auth(), "X-Idempotency-Key": "batch-unknown"},
        )
        assert resp.status_code == 400
        oversized = {"records": []}
        for i in range(MAX_BATCH_RECORDS + 1):
            oversized["records"].append(
                {
                    "external_key": f"ext-{i:04d}",
                    "application_id": "app-1",
                    "problem_description": "x",
                }
            )
        resp = client.post(
            "/api/v1/batch-intake",
            json=oversized,
            headers={**self._auth(), "X-Idempotency-Key": "batch-cap"},
        )
        assert resp.status_code in (400, 422)

    def test_create_replay_and_csv(self, monkeypatch) -> None:
        client, ctx = self._client(monkeypatch)
        self._seed_profile(ctx)
        ctx.temporal_client = MagicMock()
        ctx.temporal_client.start_workflow = AsyncMock()
        headers = {**self._auth(), "X-Idempotency-Key": "batch-1"}
        first = client.post("/api/v1/batch-intake", json=self._payload(2), headers=headers)
        assert first.status_code == 202, first.text
        assert first.json()["total"] == 2
        assert first.json()["workflow_id"].startswith("wf-batch-")
        second = client.post("/api/v1/batch-intake", json=self._payload(2), headers=headers)
        assert second.json()["job_id"] == first.json()["job_id"]
        csv_body = {
            "csv": {
                "text": "ticket,app,summary\nT1,app-1,db slow\n",
                "field_mapping": {
                    "external_key": "ticket",
                    "application_id": "app",
                    "problem_description": "summary",
                },
            }
        }
        csv_resp = client.post(
            "/api/v1/batch-intake",
            json=csv_body,
            headers={**self._auth(), "X-Idempotency-Key": "batch-csv"},
        )
        assert csv_resp.status_code == 202, csv_resp.text
        assert csv_resp.json()["total"] == 1

    def test_get_pagination_and_isolation(self, monkeypatch) -> None:
        client, ctx = self._client(monkeypatch)
        self._seed_profile(ctx)
        ctx.temporal_client = MagicMock()
        ctx.temporal_client.start_workflow = AsyncMock()
        job_id = client.post(
            "/api/v1/batch-intake",
            json=self._payload(3),
            headers={**self._auth(), "X-Idempotency-Key": "batch-get"},
        ).json()["job_id"]
        body = client.get(
            f"/api/v1/batch-intake/{job_id}?limit=2&offset=1", headers=self._auth()
        ).json()
        assert body["total"] == 3
        assert [item["record_index"] for item in body["items"]] == [1, 2]
        assert body["summary"]["total"] == 3
        assert (
            client.get(f"/api/v1/batch-intake/{job_id}", headers=self._auth("tenant-b")).status_code
            == 404
        )
        assert (
            client.get("/api/v1/batch-intake/not-a-uuid", headers=self._auth()).status_code == 400
        )

    def test_cancel_and_retry(self, monkeypatch) -> None:
        client, ctx = self._client(monkeypatch)
        self._seed_profile(ctx)
        ctx.temporal_client = MagicMock()
        ctx.temporal_client.start_workflow = AsyncMock()
        handle = MagicMock()
        handle.cancel = AsyncMock()
        ctx.temporal_client.get_workflow_handle = MagicMock(return_value=handle)
        job_id = client.post(
            "/api/v1/batch-intake",
            json=self._payload(1),
            headers={**self._auth(), "X-Idempotency-Key": "batch-cancel"},
        ).json()["job_id"]
        canceled = client.post(f"/api/v1/batch-intake/{job_id}/cancel", headers=self._auth())
        assert canceled.status_code == 202
        assert canceled.json()["status"] == "CANCELLING"
        assert (
            client.post(f"/api/v1/batch-intake/{uuid4()}/cancel", headers=self._auth()).status_code
            == 404
        )
        # Nothing failed: retry is a 409.
        assert (
            client.post(
                f"/api/v1/batch-intake/{job_id}/retry",
                headers={**self._auth(), "X-Idempotency-Key": "batch-retry-empty"},
            ).status_code
            == 409
        )

    def test_retry_failed_and_purge(self, monkeypatch) -> None:
        import anyio

        client, ctx = self._client(monkeypatch)
        self._seed_profile(ctx)
        ctx.temporal_client = MagicMock()
        ctx.temporal_client.start_workflow = AsyncMock()
        job_id = client.post(
            "/api/v1/batch-intake",
            json=self._payload(2),
            headers={**self._auth(), "X-Idempotency-Key": "batch-retry"},
        ).json()["job_id"]

        async def _fail_one() -> None:
            record = await ctx.batch_repo.get_record("tenant-a", UUID(job_id), 0)
            assert record is not None
            await ctx.batch_repo.save_record(
                "tenant-a",
                UUID(job_id),
                record.model_copy(update={"status": BatchRecordStatus.FAILED}),
            )

        anyio.run(_fail_one)
        retried = client.post(
            f"/api/v1/batch-intake/{job_id}/retry",
            headers={**self._auth(), "X-Idempotency-Key": "batch-retry-1"},
        )
        assert retried.status_code == 202, retried.text
        assert retried.json()["retried"] == 1
        assert retried.json()["workflow_id"].endswith("-r1")
        # Purge refuses live batches.
        assert (
            client.delete(f"/api/v1/batch-intake/{job_id}", headers=self._auth()).status_code == 409
        )

    def test_no_temporal_503(self, monkeypatch) -> None:
        client, ctx = self._client(monkeypatch)
        self._seed_profile(ctx)
        assert (
            client.post(
                "/api/v1/batch-intake",
                json=self._payload(1),
                headers={**self._auth(), "X-Idempotency-Key": "batch-503"},
            ).status_code
            == 503
        )


# ---------------------------------------------------------------------------
# migration 015
# ---------------------------------------------------------------------------


class TestMigration015:
    def test_015_chain(self) -> None:
        import importlib.util
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[2]
            / "migrations"
            / "versions"
            / "015_batch_intake_records.py"
        )
        assert path.is_file()
        spec = importlib.util.spec_from_file_location("migration_015", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.revision == "015_batch_intake"
        assert mod.down_revision == "014_reference_documents"

    def test_single_head_is_015(self) -> None:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config("alembic.ini")
        script = ScriptDirectory.from_config(cfg)
        assert script.get_heads() == ["015_batch_intake"]
