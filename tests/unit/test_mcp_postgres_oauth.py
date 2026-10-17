# tests/unit/test_mcp_postgres_oauth.py
"""P3: MCP postgres gateway, Spring derivation, OAuth bridge, fan-out."""

import pytest

from investigation_agent_platform.infrastructure.evidence.mcp_connections import (
    derive_connection_entry,
    derive_from_spring_datasource,
)


def test_derive_postgres_connection_entry():
    entry = derive_connection_entry(
        "jdbc:postgresql://dbhost:5432/iapdb", "iap", tenants=["n-a"]
    )
    assert entry["kind"] == "postgres"
    assert entry["host"] == "dbhost"
    assert entry["database"] == "iapdb"
    assert entry["username"] == "iap"
    assert entry["transport"] == "sse"
    assert entry["query_tool"] == "execute_sql"
    assert "password" not in str(entry).lower()


def test_derive_strips_embedded_credentials():
    entry = derive_connection_entry(
        "jdbc:postgresql://user:secret@dbhost/iapdb", "user", tenants=["n-a"]
    )
    assert "secret" not in str(entry)


def test_derive_oracle_connection_entry():
    entry = derive_connection_entry(
        "jdbc:oracle:thin:@//orahost:1521/ORCL", "ora", tenants=["n-a"]
    )
    assert entry["kind"] == "oracle"
    assert entry["host"] == "orahost"


def test_derive_from_spring_mapping_missing_url_returns_none():
    assert derive_from_spring_datasource({"username": "u"}) is None


def test_oauth_bridge_returns_none_without_env(monkeypatch):
    from investigation_agent_platform.infrastructure.security.oauth_bridge import (
        build_oauth_provider_from_env,
    )

    for var in (
        "IAP_OAUTH_PROVIDER_ID",
        "IAP_OAUTH_TOKEN_ENDPOINT",
        "IAP_OAUTH_CLIENT_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    assert build_oauth_provider_from_env() is None


@pytest.mark.asyncio
async def test_oauth_bridge_token_passthrough():
    from investigation_agent_platform.infrastructure.security.oauth_bridge import (
        TenantScopedOAuthProvider,
    )

    class _Token:
        access_token = "tok-123"

    class _Provider:
        async def get_token(self, scope, provider_id):
            assert provider_id == "p1"
            assert scope.tenant_id == "tenant-a"
            return _Token()

    bridge = TenantScopedOAuthProvider(_Provider(), "p1")
    assert await bridge.get_token("tenant-a") == "tok-123"


@pytest.mark.asyncio
async def test_oauth_bridge_failure_returns_none():
    from investigation_agent_platform.infrastructure.security.oauth_bridge import (
        TenantScopedOAuthProvider,
    )

    class _Failing:
        async def get_token(self, scope, provider_id):
            raise RuntimeError("down")

    bridge = TenantScopedOAuthProvider(_Failing(), "p1")
    assert await bridge.get_token("tenant-a") is None


def test_mcp_adapter_serves_tenant():
    from investigation_agent_platform.infrastructure.evidence.mcp import (
        McpEvidenceAdapter,
    )

    adapter = McpEvidenceAdapter(
        server_params=None,
        allowed_tools=["execute_sql"],
        tenant_allowlist={"n-a": {"iap-app-db"}},
    )
    assert adapter.serves_tenant("n-a") is True
    assert adapter.serves_tenant("other") is False
    open_adapter = McpEvidenceAdapter(server_params=None, allowed_tools=["execute_sql"])
    assert open_adapter.serves_tenant("anyone") is True


def test_mcp_adapter_rejects_unknown_transport():
    import pytest as _pytest

    from investigation_agent_platform.infrastructure.evidence.mcp import (
        McpEvidenceAdapter,
    )

    with _pytest.raises(ValueError):
        McpEvidenceAdapter(
            server_params=None, allowed_tools=["execute_sql"], transport="grpc"
        )
