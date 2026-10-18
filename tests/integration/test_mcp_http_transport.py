# tests/integration/test_mcp_http_transport.py
"""Part 9 Phase 6: Streamable HTTP transport with per-request authentication.

Exercises the mounted ASGI app end to end: JWT/header identity resolution,
investigation header validation, tool dispatch with the bound context, and
transport-tier rejection (401/400) before any tool runs.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

from starlette.testclient import TestClient

from investigation_agent_platform.domain.evidence.completeness import EvidenceCompleteness
from investigation_agent_platform.infrastructure.configuration.config import McpConfig
from investigation_agent_platform.mcp.server import create_mcp_http_app

TENANT = "tenant-a"


def _services_with_search():  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.investigation.runtime_evidence import (
        RuntimeEvidenceSearchResult,
    )

    services = MagicMock()
    services.runtime_evidence.search = AsyncMock(
        return_value=RuntimeEvidenceSearchResult(
            items=[],
            next_cursor=None,
            has_more=False,
            total_count=0,
            completeness=EvidenceCompleteness(complete=True, returned_count=0, examined_count=0),
        )
    )
    return services


def _client(monkeypatch, services=None):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("IAP_ENVIRONMENT", "development")
    for var in ("IAP_AUTH_JWKS_URL", "IAP_AUTH_ISSUER", "IAP_AUTH_AUDIENCE"):
        monkeypatch.delenv(var, raising=False)
    app = create_mcp_http_app(services or _services_with_search(), McpConfig())
    # Context-manager form runs the app lifespan (the SDK session manager
    # requires it); callers must use `with _client(...) as client:`. The base
    # URL uses localhost so the SDK DNS-rebinding protection accepts the host.
    return TestClient(app, raise_server_exceptions=False, base_url="http://localhost:8899")


def _headers(iid=None, tenant=TENANT, correlation="corr-1"):  # type: ignore[no-untyped-def]
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "X-Tenant-ID": tenant,
        "X-Correlation-ID": correlation,
    }
    if iid is not None:
        headers["X-Investigation-ID"] = str(iid)
    return headers


def _initialize(client, headers):  # type: ignore[no-untyped-def]
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "contract-test", "version": "1.0"},
            },
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    session_id = response.headers.get("mcp-session-id")
    assert session_id, "server must issue a session id"
    initialized = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={**headers, "mcp-session-id": session_id},
    )
    assert initialized.status_code in (200, 202), initialized.text
    return session_id


def _search_body():  # type: ignore[no-untyped-def]
    return {
        "environment": "production",
        "identifiers": None,
        "keywords": ["timeout"],
        "services": None,
        "severities": None,
        "start_time": None,
        "end_time": None,
        "limit": 10,
        "cursor": None,
    }


def _sse_result(response):  # type: ignore[no-untyped-def]
    """Extract the JSON-RPC result from a streamable-HTTP SSE response."""
    import json as _json

    for line in response.text.splitlines():
        if line.startswith("data: "):
            payload = _json.loads(line[len("data: ") :])
            if "result" in payload:
                return payload["result"]
    raise AssertionError(f"no result frame in SSE response: {response.text[:500]}")


def test_http_search_round_trip_with_dev_identity(monkeypatch):
    iid = uuid.uuid4()
    services = _services_with_search()
    with _client(monkeypatch, services) as client:
        headers = _headers(iid=iid)
        session_id = _initialize(client, headers)
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "search_runtime_evidence", "arguments": _search_body()},
            },
            headers={**headers, "mcp-session-id": session_id},
        )
        assert response.status_code == 200, response.text
        result = _sse_result(response)
        assert "error" not in result.get("structuredContent", {})
        structured = result["structuredContent"]["result"]
        assert structured["completeness"]["complete"] is True
        (scope_arg, _) = services.runtime_evidence.search.call_args.args
        assert scope_arg.tenant_id == TENANT and scope_arg.investigation_id == iid
        assert scope_arg.correlation_id == "corr-1"


def test_http_missing_identity_rejected_before_dispatch(monkeypatch):
    services = _services_with_search()
    with _client(monkeypatch, services) as client:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers=headers,
        )
        assert response.status_code == 401
    assert services.runtime_evidence.search.call_count == 0


def test_http_malformed_investigation_rejected(monkeypatch):
    services = _services_with_search()
    with _client(monkeypatch, services) as client:
        headers = _headers()
        headers["X-Investigation-ID"] = "not-a-uuid"
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers=headers,
        )
        assert response.status_code == 400
    assert services.runtime_evidence.search.call_count == 0


def test_http_missing_investigation_rejected(monkeypatch):
    services = _services_with_search()
    with _client(monkeypatch, services) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers=_headers(iid=None),
        )
        assert response.status_code == 400
    assert services.runtime_evidence.search.call_count == 0
