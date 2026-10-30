# tests/unit/test_ops_wire.py
"""Golden wire tests for jobs/clusters/knowledge routers (OpenAPI spec 1.0)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import AppContext, set_app_context
from investigation_agent_platform.domain.common.background_job import BackgroundJob

HEADERS = {"X-Tenant-ID": "tenant-a"}


@pytest.fixture
def _restore_context():
    import investigation_agent_platform.api.dependencies as deps

    prev = deps._context
    yield
    deps._context = prev


def _client():
    ctx = AppContext()
    set_app_context(ctx)
    return ctx, TestClient(create_app())


class TestJobsWire:
    def test_list_empty_shape(self, _restore_context: None) -> None:
        _, client = _client()
        body = client.get("/api/v1/jobs", headers=HEADERS).json()
        assert body == {"items": [], "total": 0, "offset": 0, "limit": 50}

    def test_detail_wrapper_shape(self, _restore_context: None) -> None:
        import anyio

        ctx, client = _client()
        job = BackgroundJob(tenant_id="tenant-a", kind="test-kind")

        async def _save() -> None:
            await ctx.background_job_repo.create(job)

        anyio.run(_save)
        body = client.get(f"/api/v1/jobs/{job.id}", headers=HEADERS).json()
        assert set(body) == {"job"}
        # Nested dump must be COMPLETE (defaults included) — exclude_unset
        # must not sparse-emit models built with defaulted fields.
        assert body["job"]["id"] == str(job.id)
        assert body["job"]["kind"] == "test-kind"
        assert body["job"]["status"] == "QUEUED"
        assert body["job"]["version"] == 1

    def test_unknown_job_404_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.get("/api/v1/jobs/00000000-0000-0000-0000-000000000000", headers=HEADERS)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "JOB_NOT_FOUND"


class TestClustersWire:
    def test_list_empty_shape(self, _restore_context: None) -> None:
        _, client = _client()
        body = client.get("/api/v1/clusters", headers=HEADERS).json()
        # No offset/limit keys on this route.
        assert body == {"items": [], "total": 0}

    def test_unknown_cluster_404_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.get("/api/v1/clusters/00000000-0000-0000-0000-000000000000", headers=HEADERS)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "CLUSTER_NOT_FOUND"

    def test_run_requires_key_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.post("/api/v1/clusters/run", json={}, headers=HEADERS)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


class TestKnowledgeWire:
    def test_search_empty_shape(self, _restore_context: None) -> None:
        _, client = _client()
        body = client.get("/api/v1/knowledge/search", headers=HEADERS).json()
        assert body == {"items": [], "total": 0}

    def test_detail_unknown_404_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.get(
            "/api/v1/investigations/00000000-0000-0000-0000-000000000000/knowledge",
            headers=HEADERS,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "INVESTIGATION_NOT_FOUND"
