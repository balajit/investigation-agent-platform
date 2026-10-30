# tests/unit/test_profiles_crud.py
"""Phase 1 contract tests: profile CRUD, sanitization, lifecycle, idempotency."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import AppContext, set_app_context


@pytest.fixture
def _restore_context():
    import investigation_agent_platform.api.dependencies as deps

    prev = deps._context
    yield
    deps._context = prev


def _client() -> tuple[AppContext, TestClient]:
    ctx = AppContext()
    set_app_context(ctx)
    return ctx, TestClient(create_app())


def _body(app_id: str = "svc-a", **overrides):
    doc = {
        "id": app_id,
        "name": "Service A",
        "description": "contract test profile",
        "environment": "production",
        "owner": "team-a",
        "mapping_source_id": "generic-ecs",
        "services": [
            {
                "name": "svc-a",
                "code": {"org": "acme", "repos": ["svc-a"], "source_roots": ["src"]},
                "telemetry": {"log_indices": ["logs-a-*"], "service_names": ["svc-a"]},
            }
        ],
        "datasources": [
            {
                "id": "primary",
                "name": "db",
                "kind": "postgres",
                "role": "state",
                "allowed_tables": ["t"],
                "query_templates": {
                    "find": {
                        "description": "find",
                        "sql": "SELECT * FROM t WHERE id = :id",
                        "allowed_tables": ["t"],
                    }
                },
            }
        ],
    }
    doc.update(overrides)
    return doc


HEADERS = {"X-Tenant-ID": "tenant-a"}


class TestCreateProfile:
    def test_create_draft_sanitized(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.post("/api/v1/profiles", json=_body(), headers=HEADERS)
        assert resp.status_code == 201, resp.text
        payload = resp.json()["profile"]
        ds = payload["datasources"][0]
        assert "sql" not in ds["query_templates"]["find"]
        assert ds["query_templates"]["find"]["description"] == "find"

    def test_create_duplicate_same_content_replays_200(self, _restore_context: None) -> None:
        _, client = _client()
        first = client.post("/api/v1/profiles", json=_body(), headers=HEADERS)
        assert first.status_code == 201
        second = client.post("/api/v1/profiles", json=_body(), headers=HEADERS)
        assert second.status_code == 200
        assert second.json()["idempotent_replay"] is True

    def test_create_duplicate_different_content_conflicts(self, _restore_context: None) -> None:
        _, client = _client()
        assert client.post("/api/v1/profiles", json=_body(), headers=HEADERS).status_code == 201
        altered = _body()
        altered["description"] = "changed"
        resp = client.post("/api/v1/profiles", json=altered, headers=HEADERS)
        assert resp.status_code == 409

    def test_create_non_version_1_rejected(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.post("/api/v1/profiles", json=_body(version=3), headers=HEADERS)
        assert resp.status_code == 409

    def test_create_tenant_mismatch_rejected(self, _restore_context: None) -> None:
        _, client = _client()
        body = _body()
        body["tenant_id"] = "tenant-evil"
        resp = client.post("/api/v1/profiles", json=body, headers=HEADERS)
        assert resp.status_code == 400

    def test_create_unknown_mapping_rejected(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.post(
            "/api/v1/profiles", json=_body(mapping_source_id="nope"), headers=HEADERS
        )
        assert resp.status_code == 422

    def test_create_unknown_capability_rejected(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.post(
            "/api/v1/profiles",
            json=_body(allowed_capabilities=["MIND_READ"]),
            headers=HEADERS,
        )
        assert resp.status_code == 422

    def test_create_active_without_services_rejected(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.post(
            "/api/v1/profiles",
            json=_body(status="ACTIVE", services=[]),
            headers=HEADERS,
        )
        assert resp.status_code == 422

    def test_create_secret_connection_ref_rejected(self, _restore_context: None) -> None:
        _, client = _client()
        body = _body()
        body["datasources"][0]["connection_ref"] = "postgresql://user:pw@host/db"
        resp = client.post("/api/v1/profiles", json=body, headers=HEADERS)
        assert resp.status_code == 400

    def test_create_with_idempotency_key_replays(self, _restore_context: None) -> None:
        _, client = _client()
        headers = {**HEADERS, "X-Idempotency-Key": "k-1"}
        first = client.post("/api/v1/profiles", json=_body(), headers=headers)
        assert first.status_code == 201
        second = client.post("/api/v1/profiles", json=_body(), headers=headers)
        assert second.status_code == 200


class TestReadProfiles:
    def test_get_draft_only_app_returns_latest_draft(self, _restore_context: None) -> None:
        # Consumers must retrieve DRAFTs: detail returns the latest revision
        # whatever its status (investigations resolve ACTIVE separately).
        _, client = _client()
        client.post("/api/v1/profiles", json=_body(), headers=HEADERS)
        resp = client.get("/api/v1/profiles/svc-a", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json()["version"] == 1
        assert resp.json()["profile"]["status"] == "DRAFT"

    def test_get_latest_active_by_default(self, _restore_context: None) -> None:
        _, client = _client()
        client.post("/api/v1/profiles", json=_body(), headers=HEADERS)
        client.put("/api/v1/profiles/svc-a", json=_body(status="ACTIVE"), headers=HEADERS)
        resp = client.get("/api/v1/profiles/svc-a", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json()["version"] == 2
        assert resp.json()["profile"]["status"] == "ACTIVE"

    def test_get_unknown_app_404(self, _restore_context: None) -> None:
        _, client = _client()
        assert client.get("/api/v1/profiles/nope", headers=HEADERS).status_code == 404

    def test_get_pinned_version(self, _restore_context: None) -> None:
        _, client = _client()
        client.post("/api/v1/profiles", json=_body(), headers=HEADERS)
        resp = client.get("/api/v1/profiles/svc-a?version=1", headers=HEADERS)
        # v1 is DRAFT: exact pin still resolves (no silent fallback involved).
        assert resp.status_code == 200
        assert resp.json()["version"] == 1

    def test_get_unknown_version_404(self, _restore_context: None) -> None:
        _, client = _client()
        client.post("/api/v1/profiles", json=_body(), headers=HEADERS)
        assert client.get("/api/v1/profiles/svc-a?version=9", headers=HEADERS).status_code == 404

    def test_list_status_filter(self, _restore_context: None) -> None:
        _, client = _client()
        client.post("/api/v1/profiles", json=_body(), headers=HEADERS)
        all_items = client.get("/api/v1/profiles", headers=HEADERS).json()
        assert any(i["id"] == "svc-a" for i in all_items["items"])
        active = client.get("/api/v1/profiles?status=ACTIVE", headers=HEADERS).json()
        assert all(i["status"] == "ACTIVE" for i in active["items"])
        assert not any(i["id"] == "svc-a" for i in active["items"])

    def test_revisions_summaries(self, _restore_context: None) -> None:
        _, client = _client()
        client.post("/api/v1/profiles", json=_body(), headers=HEADERS)
        client.put("/api/v1/profiles/svc-a", json=_body(status="ACTIVE"), headers=HEADERS)
        resp = client.get("/api/v1/profiles/svc-a/revisions", headers=HEADERS)
        assert resp.status_code == 200
        assert [i["version"] for i in resp.json()["items"]] == [2, 1]


class TestUpdateProfile:
    def test_illegal_transition_rejected(self, _restore_context: None) -> None:
        _, client = _client()
        client.post("/api/v1/profiles", json=_body(status="ACTIVE"), headers=HEADERS)
        resp = client.put("/api/v1/profiles/svc-a", json=_body(status="DRAFT"), headers=HEADERS)
        assert resp.status_code == 409

    def test_body_version_mismatch_rejected(self, _restore_context: None) -> None:
        _, client = _client()
        client.post("/api/v1/profiles", json=_body(), headers=HEADERS)
        resp = client.put(
            "/api/v1/profiles/svc-a", json=_body(status="ACTIVE", version=7), headers=HEADERS
        )
        assert resp.status_code == 409

    def test_update_unknown_app_404(self, _restore_context: None) -> None:
        _, client = _client()
        assert client.put("/api/v1/profiles/nope", json=_body(), headers=HEADERS).status_code == 404


class TestDeleteProfile:
    def test_delete_draft_only_app(self, _restore_context: None) -> None:
        _, client = _client()
        client.post("/api/v1/profiles", json=_body(), headers=HEADERS)
        resp = client.delete("/api/v1/profiles/svc-a", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json() == {"id": "svc-a", "version": None, "deleted": 1}
        assert client.get("/api/v1/profiles/svc-a", headers=HEADERS).status_code == 404
        assert client.get("/api/v1/profiles/svc-a/revisions", headers=HEADERS).status_code == 404
        assert all(
            i["id"] != "svc-a"
            for i in client.get("/api/v1/profiles", headers=HEADERS).json()["items"]
        )

    def test_delete_unknown_app_404(self, _restore_context: None) -> None:
        _, client = _client()
        assert client.delete("/api/v1/profiles/nope", headers=HEADERS).status_code == 404

    def test_delete_app_with_active_history_409_and_kept(self, _restore_context: None) -> None:
        _, client = _client()
        client.post("/api/v1/profiles", json=_body(), headers=HEADERS)
        client.put("/api/v1/profiles/svc-a", json=_body(status="ACTIVE"), headers=HEADERS)
        resp = client.delete("/api/v1/profiles/svc-a", headers=HEADERS)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "PROFILE_NOT_DELETABLE"
        revs = client.get("/api/v1/profiles/svc-a/revisions", headers=HEADERS).json()
        assert revs["total"] == 2

    def test_delete_single_draft_revision_over_active(self, _restore_context: None) -> None:
        import anyio

        ctx, client = _client()
        client.post("/api/v1/profiles", json=_body(), headers=HEADERS)
        client.put("/api/v1/profiles/svc-a", json=_body(status="ACTIVE"), headers=HEADERS)

        async def _seed_draft_v3() -> None:
            from investigation_agent_platform.domain.profile.models import ProfileStatus

            revs = await ctx.profile_repo.list_revisions("tenant-a", "svc-a")
            draft = revs[0].model_copy(update={"version": 3, "status": ProfileStatus.DRAFT})
            await ctx.profile_repo.save("tenant-a", draft)

        anyio.run(_seed_draft_v3)
        resp = client.delete("/api/v1/profiles/svc-a?version=3", headers=HEADERS)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"id": "svc-a", "version": 3, "deleted": 1}
        detail = client.get("/api/v1/profiles/svc-a", headers=HEADERS).json()
        assert detail["version"] == 2
        assert detail["profile"]["status"] == "ACTIVE"

    def test_delete_single_non_draft_409(self, _restore_context: None) -> None:
        _, client = _client()
        client.post("/api/v1/profiles", json=_body(), headers=HEADERS)
        client.put("/api/v1/profiles/svc-a", json=_body(status="ACTIVE"), headers=HEADERS)
        resp = client.delete("/api/v1/profiles/svc-a?version=2", headers=HEADERS)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "PROFILE_NOT_DELETABLE"

    def test_delete_missing_version_404(self, _restore_context: None) -> None:
        _, client = _client()
        client.post("/api/v1/profiles", json=_body(), headers=HEADERS)
        assert client.delete("/api/v1/profiles/svc-a?version=9", headers=HEADERS).status_code == 404

    def test_delete_idempotent_replay(self, _restore_context: None) -> None:
        _, client = _client()
        client.post("/api/v1/profiles", json=_body(), headers=HEADERS)
        headers = {**HEADERS, "X-Idempotency-Key": "del-1"}
        first = client.delete("/api/v1/profiles/svc-a", headers=headers)
        assert first.status_code == 200
        second = client.delete("/api/v1/profiles/svc-a", headers=headers)
        assert second.status_code == 200
        assert second.json() == first.json()


class TestLifecycleGates:
    def test_deprecated_blocks_new_investigations(self, _restore_context: None) -> None:
        _, client = _client()
        # Full bodies round-trip (sanitized GET output intentionally cannot:
        # template SQL is never readable, so updates re-supply it).
        assert (
            client.post(
                "/api/v1/profiles", json=_body(status="ACTIVE"), headers=HEADERS
            ).status_code
            == 201
        )
        up = client.put("/api/v1/profiles/svc-a", json=_body(status="DEPRECATED"), headers=HEADERS)
        assert up.status_code == 200, up.text
        resp = client.post(
            "/api/v1/investigations",
            json={
                "application_id": "svc-a",
                "problem_description": "blocked?",
                "session_id": "sess-x",
            },
            headers=HEADERS,
        )
        assert resp.status_code == 409
