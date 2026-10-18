# tests/unit/test_profiles_spring.py
"""Phase 2 contract tests: Spring derive / refresh / drift endpoints."""

from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import AppContext, set_app_context
from investigation_agent_platform.integrations.spring_config.exceptions import (
    ConfigNotFoundError,
    ConfigRateLimitError,
)
from investigation_agent_platform.integrations.spring_config.models import ConfigResult

# NOTE: api.v1.routers.__init__ rebinds `profiles` to the APIRouter, so the
# submodule must come from sys.modules (it IS imported by create_app below).
_profiles_module = sys.modules["investigation_agent_platform.api.v1.routers.profiles"]

HEADERS = {"X-Tenant-ID": "tenant-a"}


@pytest.fixture
def _restore_context():
    import investigation_agent_platform.api.dependencies as deps

    prev = deps._context
    yield
    deps._context = prev


@pytest.fixture
def _stub_fetch(monkeypatch):
    state = {"properties": None, "error": None}

    def _fake(binding):
        if state["error"] is not None:
            raise state["error"]
        return ConfigResult(
            application=binding.application,
            profile=binding.profile,
            label=binding.label,
            properties=dict(state["properties"] or {}),
            property_sources=("stub",),
        )

    monkeypatch.setattr(_profiles_module, "_fetch_config", _fake)
    return state


def _client():
    ctx = AppContext()
    set_app_context(ctx)
    return ctx, TestClient(create_app())


def _derive_body(**overrides):
    doc = {
        "tenant_app_id": "shop-app",
        "spring": {
            "server_url": "https://cfg.example",
            "application": "shop",
            "profile": "prod",
        },
    }
    doc.update(overrides)
    return doc


def _props(**overrides):
    base = {
        "spring.application.name": "checkout",
        "spring.profiles.active": "prod",
        "spring.datasource.url": "jdbc:postgresql://db.internal:5432/shopdb",
    }
    base.update(overrides)
    return base


class TestDeriveEndpoint:
    def test_derive_persists_draft(self, _restore_context: None, _stub_fetch) -> None:
        _stub_fetch["properties"] = _props()
        _, client = _client()
        resp = client.post(
            "/api/v1/profiles:derive-from-spring", json=_derive_body(), headers=HEADERS
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["persisted"] is True
        assert body["version"] == 1
        assert body["profile"]["status"] == "DRAFT"
        assert body["profile"]["services"][0]["name"] == "checkout"
        assert body["derivation"]["property_count"] == len(_props())
        assert body["warnings"]

    def test_derive_preview_without_persist(self, _restore_context: None, _stub_fetch) -> None:
        _stub_fetch["properties"] = _props()
        _, client = _client()
        resp = client.post(
            "/api/v1/profiles:derive-from-spring",
            json=_derive_body(persist=False),
            headers=HEADERS,
        )
        assert resp.status_code == 201
        assert resp.json()["persisted"] is False
        revs = client.get("/api/v1/profiles/shop-app/revisions", headers=HEADERS)
        assert revs.status_code == 404

    def test_derive_missing_binding_rejected(self, _restore_context: None, _stub_fetch) -> None:
        _, client = _client()
        resp = client.post(
            "/api/v1/profiles:derive-from-spring", json={"tenant_app_id": "x"}, headers=HEADERS
        )
        assert resp.status_code == 400

    def test_spring_not_found_maps_404(self, _restore_context: None, _stub_fetch) -> None:
        _stub_fetch["error"] = ConfigNotFoundError("missing")
        _, client = _client()
        resp = client.post(
            "/api/v1/profiles:derive-from-spring", json=_derive_body(), headers=HEADERS
        )
        assert resp.status_code == 404
        body = resp.json()["error"]
        assert body["code"] == "SPRING_NOT_FOUND"
        assert body["correlation_id"]

    def test_spring_throttled_maps_429_with_retry_after(
        self, _restore_context: None, _stub_fetch
    ) -> None:
        _stub_fetch["error"] = ConfigRateLimitError("slow", retry_after=7.0)
        _, client = _client()
        resp = client.post(
            "/api/v1/profiles:derive-from-spring", json=_derive_body(), headers=HEADERS
        )
        assert resp.status_code == 429
        assert resp.headers.get("retry-after") == "7.0"


class TestRefreshAndDrift:
    def test_refresh_no_change(self, _restore_context: None, _stub_fetch) -> None:
        _stub_fetch["properties"] = _props()
        _, client = _client()
        client.post("/api/v1/profiles:derive-from-spring", json=_derive_body(), headers=HEADERS)
        resp = client.post("/api/v1/profiles/shop-app:refresh-from-spring", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json() == {"changed": False, "version": 1}

    def test_refresh_material_change_writes_revision(
        self, _restore_context: None, _stub_fetch
    ) -> None:
        _stub_fetch["properties"] = _props()
        _, client = _client()
        client.post("/api/v1/profiles:derive-from-spring", json=_derive_body(), headers=HEADERS)
        _stub_fetch["properties"] = _props(**{"spring.kafka.bootstrap-servers": "k:9092"})
        resp = client.post("/api/v1/profiles/shop-app:refresh-from-spring", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["changed"] is True
        assert body["version"] == 2
        assert body["diff"]

    def test_drift_preview_without_write(self, _restore_context: None, _stub_fetch) -> None:
        _stub_fetch["properties"] = _props()
        _, client = _client()
        client.post("/api/v1/profiles:derive-from-spring", json=_derive_body(), headers=HEADERS)
        _stub_fetch["properties"] = _props(**{"spring.kafka.bootstrap-servers": "k:9092"})
        resp = client.get("/api/v1/profiles/shop-app:spring-drift", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json()["changed"] is True
        revs = client.get("/api/v1/profiles/shop-app/revisions", headers=HEADERS).json()
        assert revs["total"] == 1  # no write happened

    def test_refresh_without_binding_rejected(
        self, _restore_context: None, _stub_fetch
    ) -> None:
        _, client = _client()
        resp = client.post("/api/v1/profiles/example-app:refresh-from-spring", headers=HEADERS)
        assert resp.status_code == 422
