# tests/unit/test_tail_wire.py
"""Golden wire tests for chat/capabilities/health/aggregate/batch (OpenAPI spec 1.0)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import AppContext, set_app_context

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


class TestChatWire:
    def test_create_session_shape(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.post(
            "/api/v1/chat/sessions",
            json={},
            headers={**HEADERS, "X-Idempotency-Key": "chat-wire-1"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert set(body) == {"session"}
        assert body["session"]["tenant_id"] == "tenant-a"

    def test_create_requires_key_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.post("/api/v1/chat/sessions", json={}, headers=HEADERS)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    def test_messages_empty_shape(self, _restore_context: None) -> None:
        _, client = _client()
        created = client.post(
            "/api/v1/chat/sessions",
            json={},
            headers={**HEADERS, "X-Idempotency-Key": "chat-wire-2"},
        ).json()
        session_id = created["session"]["id"]
        body = client.get(f"/api/v1/chat/sessions/{session_id}/messages", headers=HEADERS).json()
        assert body == {"items": [], "total": 0, "offset": 0, "limit": 100}

    def test_unknown_session_404_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.get(
            "/api/v1/chat/sessions/00000000-0000-0000-0000-000000000000/messages",
            headers=HEADERS,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "SESSION_NOT_FOUND"


class TestCapabilitiesHealthWire:
    def test_capabilities_shape(self, _restore_context: None) -> None:
        _, client = _client()
        body = client.get("/api/v1/capabilities", headers=HEADERS).json()
        assert set(body) == {"items", "total"}
        assert body["total"] == len(body["items"]) > 0
        assert set(body["items"][0]) == {
            "capability_id",
            "contract_version",
            "status",
            "modes",
            "limits",
        }

    def test_live_exact(self, _restore_context: None) -> None:
        _, client = _client()
        assert client.get("/api/v1/health/live").json() == {"status": "UP"}


class TestAggregateWire:
    def test_latest_without_store_503_envelope(self, _restore_context: None) -> None:
        ctx, client = _client()
        ctx.artifact_store = None
        resp = client.get("/api/v1/aggregate-reports/latest", headers=HEADERS)
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "ARTIFACT_STORE_UNAVAILABLE"

    def test_build_requires_key_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.post("/api/v1/aggregate-reports", json={}, headers=HEADERS)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


class TestBatchWire:
    def test_submit_requires_key_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.post("/api/v1/batch-intake", json={"records": []}, headers=HEADERS)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    def test_detail_unknown_404_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.get(
            "/api/v1/batch-intake/00000000-0000-0000-0000-000000000000", headers=HEADERS
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "BATCH_NOT_FOUND"

    def test_purge_unknown_409_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.delete(
            "/api/v1/batch-intake/00000000-0000-0000-0000-000000000000", headers=HEADERS
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "BATCH_PURGE_REFUSED"
