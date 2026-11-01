"""AD1: the API rate-limit key is the caller-supplied tenant header.

Pre-auth the bucket is keyed by transport peer (never a header); post-auth
the verified identity keys the tenant+principal bucket. Written first, red
before the fix.
"""

from __future__ import annotations


def _client():  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from investigation_agent_platform.api.app import create_app
    from investigation_agent_platform.api.dependencies import AppContext, set_app_context

    ctx = AppContext()
    set_app_context(ctx)
    return TestClient(create_app()), ctx


def test_ad1_t01_spoofed_header_cannot_split_the_peer_bucket(monkeypatch) -> None:
    import investigation_agent_platform.api.rate_limit as rl

    monkeypatch.setenv("IAP_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setattr(
        rl,
        "PRE_AUTH_QUOTAS",
        {"default": (2, 60), "investigation_create": (2, 60), "workflow_control": (60, 60)},
    )
    client, _ = _client()
    url = "/api/v1/investigations/00000000-0000-0000-0000-000000000000/evidence/00000000-0000-0000-0000-000000000001"
    # Two requests pass (peer budget 2); the third is limited even though
    # it carries a different tenant header — the bucket is keyed by peer,
    # never by header. Under the old header key none of these would 429.
    assert client.get(url, headers={"X-Tenant-ID": "tenant-a"}).status_code == 404
    assert client.get(url, headers={"X-Tenant-ID": "tenant-b"}).status_code == 404
    limited = client.get(url, headers={"X-Tenant-ID": "tenant-a"})
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"
    assert limited.headers.get("Retry-After") is not None


def test_ad1_t02_verified_requests_count_per_tenant(monkeypatch) -> None:
    import asyncio as _asyncio
    from types import SimpleNamespace

    import investigation_agent_platform.api.rate_limit as rl
    from investigation_agent_platform.api import tenant as tenant_mod
    from investigation_agent_platform.api.tenant import get_verified_identity

    monkeypatch.setenv("IAP_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("IAP_ENVIRONMENT", "test")
    monkeypatch.setitem(rl.ENDPOINT_QUOTAS, "default", (1, 60))

    seen: list = []
    real_check = rl.check_rate_limit

    async def _spy(store, tenant, principal, path, quotas=None):  # type: ignore[no-untyped-def]
        seen.append((tenant, principal))
        return await real_check(store, tenant, principal, path, quotas)

    monkeypatch.setattr(tenant_mod, "_check_rate_limit", _spy)

    class _State:
        def __init__(self, store):  # type: ignore[no-untyped-def]
            self.rate_limit_store = store

    class _App:
        def __init__(self, store):  # type: ignore[no-untyped-def]
            self.state = _State(store)

    def _request(tenant, store):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            headers={},
            app=_App(store),
            state=SimpleNamespace(),
            url=SimpleNamespace(path="/api/v1/x"),
        ), tenant, None

    async def _go():  # type: ignore[no-untyped-def]
        store = rl.InMemoryRateLimitStore()
        req, tenant, auth = _request("tenant-a", store)
        await get_verified_identity(req, tenant, auth)
        try:
            req2, tenant2, auth2 = _request("tenant-a", store)
            await get_verified_identity(req2, tenant2, auth2)
        except Exception as exc:
            return exc
        return None

    err = _asyncio.run(_go())
    assert err is not None
    assert getattr(err, "code", "") == "RATE_LIMITED"
    assert getattr(err, "http_status", None) == 429
    assert getattr(err, "headers", {}).get("Retry-After") is not None
    assert [t for t, _ in seen] == ["tenant-a", "tenant-a"]
    assert {p for _, p in seen} == {"dev-principal"}


def test_ad1_t02b_buckets_key_tenant_and_principal() -> None:
    from investigation_agent_platform.api.rate_limit import build_rate_limit_key

    assert build_rate_limit_key("t", "p1", "x") != build_rate_limit_key("t", "p2", "x")
    assert build_rate_limit_key("t1", "p", "x") != build_rate_limit_key("t2", "p", "x")
    assert build_rate_limit_key(None, None, "x") == "anonymous:anonymous:x"


def test_ad1_t03_unauthenticated_flood_still_limited(monkeypatch) -> None:
    import investigation_agent_platform.api.rate_limit as rl

    monkeypatch.setenv("IAP_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setattr(
        rl,
        "PRE_AUTH_QUOTAS",
        {"default": (2, 60), "investigation_create": (2, 60), "workflow_control": (60, 60)},
    )
    client, _ = _client()
    statuses = [
        client.get("/api/v1/investigations/00000000-0000-0000-0000-000000000000/evidence/00000000-0000-0000-0000-000000000001").status_code for _ in range(5)
    ]
    assert 429 in statuses


def test_ad1_t04_store_failure_fails_open_at_both_stages(monkeypatch, caplog) -> None:
    import asyncio as _asyncio

    from investigation_agent_platform.api.rate_limit import check_rate_limit

    class _Broken:
        async def allow(self, key, limit, window):  # type: ignore[no-untyped-def]
            raise RuntimeError("store down")

    allowed, _, _ = _asyncio.run(check_rate_limit(_Broken(), "t", "p", "/api/v1/x"))
    assert allowed is True
    assert "failing open" in caplog.text.lower()


def test_ad1_t05_limiter_off_by_default_outside_production(monkeypatch) -> None:
    from investigation_agent_platform.api.rate_limit import rate_limit_enabled

    monkeypatch.delenv("IAP_RATE_LIMIT_ENABLED", raising=False)
    monkeypatch.setenv("IAP_ENVIRONMENT", "test")
    assert rate_limit_enabled() is False
    monkeypatch.setenv("IAP_ENVIRONMENT", "production")
    assert rate_limit_enabled() is True
    monkeypatch.setenv("IAP_RATE_LIMIT_ENABLED", "false")
    assert rate_limit_enabled() is False
