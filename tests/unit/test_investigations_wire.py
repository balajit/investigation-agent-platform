# tests/unit/test_investigations_wire.py
"""Golden wire tests for investigations + events routers (OpenAPI spec 1.0).

Pins exact response bodies (including explicit nulls) and the unified
{"error": {...}} envelope across the lifecycle endpoints.
"""

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


def _create(client, **overrides):
    body = {
        "application_id": "example-app",
        "problem_description": "latency spike after deploy",
        "session_id": "sess-wire-1",
    }
    body.update(overrides)
    return client.post("/api/v1/investigations", json=body, headers=HEADERS)


class TestInvestigationsGolden:
    def test_create_body_exact(self, _restore_context: None) -> None:
        _, client = _client()
        resp = _create(client)
        assert resp.status_code == 201
        body = resp.json()
        assert set(body) == {
            "id",
            "session_id",
            "application_id",
            "status",
            "tenant_id",
            "investigation",
        }
        assert body["application_id"] == "example-app"
        assert body["tenant_id"] == "tenant-a"
        assert body["investigation"]["application_id"] == "example-app"
        assert body["investigation"]["id"] == body["id"]
        assert body["investigation"]["status"] == "CREATED"

    def test_create_replay_adds_only_flag(self, _restore_context: None) -> None:
        _, client = _client()
        headers = {**HEADERS, "X-Idempotency-Key": "wire-replay-1"}
        first = client.post(
            "/api/v1/investigations",
            json={
                "application_id": "example-app",
                "problem_description": "x",
                "session_id": "sess-r",
            },
            headers=headers,
        )
        assert first.status_code == 201
        assert "idempotent_replay" not in first.json()
        second = client.post(
            "/api/v1/investigations",
            json={
                "application_id": "example-app",
                "problem_description": "x",
                "session_id": "sess-r",
            },
            headers=headers,
        )
        assert second.status_code == 201  # replay keeps the create status (pinned)
        # Replay carries the identical payload (no envelope drift on replay).
        assert second.json()["id"] == first.json()["id"]

    def test_list_shape(self, _restore_context: None) -> None:
        _, client = _client()
        _create(client)
        body = client.get("/api/v1/investigations", headers=HEADERS).json()
        assert set(body) == {"items", "total", "offset", "limit"}
        assert body["offset"] == 0
        assert body["total"] == len(body["items"]) >= 1
        assert set(body["items"][0]) == {
            "investigation_id",
            "status",
            "application_id",
            "created_at",
            "updated_at",
        }

    def test_detail_shape(self, _restore_context: None) -> None:
        _, client = _client()
        inv_id = _create(client).json()["id"]
        body = client.get(f"/api/v1/investigations/{inv_id}", headers=HEADERS).json()
        assert set(body) == {"investigation", "pending_requirements"}
        assert body["pending_requirements"] == []

    def test_conclusion_null_pinned(self, _restore_context: None) -> None:
        _, client = _client()
        inv_id = _create(client).json()["id"]
        body = client.get(
            f"/api/v1/investigations/{inv_id}/conclusion", headers=HEADERS
        ).json()
        # conclusion:null must be PRESENT (not dropped by exclusion settings).
        assert body == {
            "investigation_id": inv_id,
            "status": "CREATED",
            "conclusion": None,
        }

    def test_findings_empty_shape(self, _restore_context: None) -> None:
        _, client = _client()
        inv_id = _create(client).json()["id"]
        body = client.get(
            f"/api/v1/investigations/{inv_id}/findings", headers=HEADERS
        ).json()
        assert body == {"items": [], "total": 0, "offset": 0, "limit": 50}

    def test_start_without_temporal_503_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        inv_id = _create(client).json()["id"]
        resp = client.post(f"/api/v1/investigations/{inv_id}/start", headers=HEADERS)
        assert resp.status_code == 503
        body = resp.json()["error"]
        assert body["code"] == "TEMPORAL_UNAVAILABLE"
        assert resp.headers.get("x-correlation-id") == body["correlation_id"]


class TestInvestigationsErrors:
    def test_unknown_investigation_404_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.get(
            "/api/v1/investigations/00000000-0000-0000-0000-000000000000", headers=HEADERS
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "INVESTIGATION_NOT_FOUND"

    def test_bad_uuid_400_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.get("/api/v1/investigations/not-a-uuid", headers=HEADERS)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_INVESTIGATION_ID"

    def test_intake_forbidden_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.post(
            "/api/v1/intake/errors",
            json={"application_id": "x", "error_class": "E"},
            headers=HEADERS,
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "INTAKE_FORBIDDEN"

    def test_fulfill_requires_key_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        inv_id = _create(client).json()["id"]
        resp = client.post(
            f"/api/v1/investigations/{inv_id}/input-requirements/00000000-0000-0000-0000-000000000000/fulfill",
            json={"requirement_version": 1, "data": {}},
            headers=HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


class TestEventsWire:
    def test_pause_without_temporal_503_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        inv_id = _create(client).json()["id"]
        resp = client.post(f"/api/v1/investigations/{inv_id}/pause", headers=HEADERS)
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "TEMPORAL_UNAVAILABLE"

    def test_pause_unknown_404_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.post(
            "/api/v1/investigations/00000000-0000-0000-0000-000000000000/pause",
            headers=HEADERS,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "INVESTIGATION_NOT_FOUND"


class TestEvidenceHypothesesTimelineWire:
    def test_evidence_empty_no_pagination_keys(self, _restore_context: None) -> None:
        _, client = _client()
        inv_id = _create(client).json()["id"]
        body = client.get(
            f"/api/v1/investigations/{inv_id}/evidence", headers=HEADERS
        ).json()
        # No offset/limit keys on this route (unlike hypotheses/timeline).
        assert body == {"items": [], "total": 0}

    def test_evidence_unknown_404_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        inv_id = _create(client).json()["id"]
        resp = client.get(
            f"/api/v1/investigations/{inv_id}/evidence/00000000-0000-0000-0000-000000000000",
            headers=HEADERS,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "EVIDENCE_NOT_FOUND"

    def test_hypotheses_empty_shape(self, _restore_context: None) -> None:
        _, client = _client()
        inv_id = _create(client).json()["id"]
        body = client.get(
            f"/api/v1/investigations/{inv_id}/hypotheses", headers=HEADERS
        ).json()
        assert body == {"items": [], "total": 0, "offset": 0, "limit": 50}

    def test_timeline_empty_shape(self, _restore_context: None) -> None:
        _, client = _client()
        inv_id = _create(client).json()["id"]
        body = client.get(
            f"/api/v1/investigations/{inv_id}/timeline", headers=HEADERS
        ).json()
        assert body == {"items": [], "total": 0, "offset": 0, "limit": 100}
