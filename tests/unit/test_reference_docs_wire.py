# tests/unit/test_reference_docs_wire.py
"""Golden wire tests for the reference-docs router (OpenAPI spec 1.0).

Pins exact response bodies, including explicit nulls (`next_cursor: null`)
that exclusion settings must preserve. Uses local-path specs on the
in-memory AppContext — no database, no embedder.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import AppContext, set_app_context
from investigation_agent_platform.domain.profile.models import ReferenceDocsProfile


def _client(monkeypatch) -> tuple[TestClient, AppContext]:
    monkeypatch.setenv("IAP_ENVIRONMENT", "development")
    ctx = AppContext()
    set_app_context(ctx)
    return TestClient(create_app()), ctx


def _auth(tenant: str = "tenant-a") -> dict[str, str]:
    return {"X-Tenant-ID": tenant}


def _seed(ctx: AppContext, tmp_path: Path) -> None:
    import anyio

    from investigation_agent_platform.api.dependencies import _DEFAULT_PROFILE

    (tmp_path / "guide.md").write_text("timeout tuning guide", encoding="utf-8")
    profile = _DEFAULT_PROFILE.model_copy(
        update={
            "id": "app-1",
            "reference_docs": ReferenceDocsProfile(
                sources=[{"kind": "local_path", "root": str(tmp_path), "paths": []}],
                local_allowed_roots=[str(tmp_path)],
            ),
        }
    )

    async def _save() -> None:
        await ctx.profile_repo.save("tenant-a", profile)

    anyio.run(_save)


class TestReferenceDocsGolden:
    def test_sources_shape_exact(self, monkeypatch, tmp_path: Path) -> None:
        client, ctx = _client(monkeypatch)
        _seed(ctx, tmp_path)
        resp = client.get(
            "/api/v1/reference-docs/sources",
            headers=_auth(),
            params={"application_id": "app-1"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert set(item) == {"source_id", "kind", "application_id"}
        assert item["kind"] == "local_path"
        assert item["application_id"] == "app-1"
        assert set(body) == {"items"}

    def test_search_empty_pins_explicit_null(self, monkeypatch, tmp_path: Path) -> None:
        client, ctx = _client(monkeypatch)
        _seed(ctx, tmp_path)
        resp = client.get("/api/v1/reference-docs/search", headers=_auth(), params={"q": "timeout"})
        assert resp.status_code == 200
        # next_cursor:null must be PRESENT (not dropped by exclusion settings).
        assert resp.json() == {"items": [], "next_cursor": None, "has_more": False}

    def test_status_shape_exact(self, monkeypatch, tmp_path: Path) -> None:
        client, ctx = _client(monkeypatch)
        _seed(ctx, tmp_path)
        sources = client.get(
            "/api/v1/reference-docs/sources",
            headers=_auth(),
            params={"application_id": "app-1"},
        ).json()
        source_id = sources["items"][0]["source_id"]
        resp = client.get(
            "/api/v1/reference-docs/status", headers=_auth(), params={"source_id": source_id}
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "tenant_id": "tenant-a",
            "source_id": source_id,
            "active_generation": 0,
            "source_revision": "",
            "chunk_count": 0,
            "status": "ACTIVE",
        }

    def test_delete_shape(self, monkeypatch, tmp_path: Path) -> None:
        client, ctx = _client(monkeypatch)
        _seed(ctx, tmp_path)
        sources = client.get(
            "/api/v1/reference-docs/sources",
            headers=_auth(),
            params={"application_id": "app-1"},
        ).json()
        source_id = sources["items"][0]["source_id"]
        resp = client.delete(f"/api/v1/reference-docs/sources/{source_id}", headers=_auth())
        assert resp.status_code == 200
        body = resp.json()
        assert body["source_id"] == source_id
        assert body["status"] == "DELETED"
        assert isinstance(body["rows_removed"], int)
        assert set(body) == {"source_id", "status", "rows_removed"}

    def test_unknown_source_zero_status(self, monkeypatch, tmp_path: Path) -> None:
        # index_status is total: unknown sources report a zero status (no 404).
        client, _ = _client(monkeypatch)
        resp = client.get(
            "/api/v1/reference-docs/status", headers=_auth(), params={"source_id": "nope"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["active_generation"] == 0
        assert body["chunk_count"] == 0

    def test_unknown_rollback_404_envelope(self, monkeypatch, tmp_path: Path) -> None:
        client, _ = _client(monkeypatch)
        resp = client.post(
            "/api/v1/reference-docs/rollback",
            json={"source_id": "nope", "generation": 1},
            headers=_auth(),
        )
        assert resp.status_code == 404
        body = resp.json()["error"]
        assert body["code"] == "REFERENCE_NOT_FOUND"
        assert body["correlation_id"]
        assert resp.headers.get("x-correlation-id") == body["correlation_id"]

    def test_reindex_requires_key_envelope(self, monkeypatch, tmp_path: Path) -> None:
        client, ctx = _client(monkeypatch)
        _seed(ctx, tmp_path)
        resp = client.post(
            "/api/v1/reference-docs/reindex",
            json={"application_id": "app-1", "source_id": "s"},
            headers=_auth(),
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "REINDEX_KEY_REQUIRED"
