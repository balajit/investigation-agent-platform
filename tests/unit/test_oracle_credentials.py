# tests/unit/test_oracle_credentials.py
"""Per-app Oracle credential wiring (oracle-mcp/apps/<app>/oracle.yaml).

Covers: dotted-name -> IAP_SECRET_ORACLE_* parity with the loader
(load-oracle-env.sh), secret:// dev-fallback for oracle-typed paths,
profile Datasource oracle/MCP shape validation, and the shipped sampledb
server entry parsing.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock

import pytest


def _dotted_to_env(db: str, field: str) -> str:
    """Mirror of load-oracle-env.sh mapping (single source is the .sh)."""
    stem = re.sub(r"[^A-Za-z0-9]+", "_", db).strip("_").upper()
    suffix = {"user_name": "USER_NAME", "password": "PASSWORD", "jdbcUrl": "JDBC_URL"}[field]
    return f"IAP_SECRET_ORACLE_{stem}_{suffix}"


class TestEnvNameParity:
    def test_loader_names_match_resolver_fallback(self) -> None:
        from investigation_agent_platform.infrastructure.secrets.resolver import (
            SecretStoreSecretResolver,
        )

        # secret:// paths use jdbc_url; the YAML/JSON face keeps `jdbcUrl`.
        cases = {
            "oracle/sample/orders/prod/user_name": "sample.orders.prod/user_name",
            "oracle/sample/orders/prod/password": "sample.orders.prod/password",
            "oracle/sample/orders/prod/jdbc_url": "sample.orders.prod/jdbcUrl",
        }
        for path, dotted_field in cases.items():
            db, _, field = dotted_field.partition("/")
            expected = SecretStoreSecretResolver.fallback_env_name(path)
            assert _dotted_to_env(db, field) == expected == (
                "IAP_SECRET_ORACLE_SAMPLE_ORDERS_PROD_"
                + {"user_name": "USER_NAME", "password": "PASSWORD", "jdbcUrl": "JDBC_URL"}[field]
            )


class TestOracleSecretPaths:
    @pytest.mark.asyncio
    async def test_dev_fallback_resolves_oracle_password(self, monkeypatch) -> None:
        from investigation_agent_platform.infrastructure.secrets.resolver import (
            resolve_connection,
        )

        monkeypatch.setenv("IAP_SECRET_ORACLE_SAMPLE_ORDERS_PROD_PASSWORD", "pw")
        out = await resolve_connection(
            "secret://oracle/sample/orders/prod/password", environment="development"
        )
        assert out == "pw"

    @pytest.mark.asyncio
    async def test_prod_plain_dsn_rejected(self) -> None:
        from investigation_agent_platform.infrastructure.secrets.resolver import (
            resolve_connection,
        )
        from investigation_agent_platform.ports.secrets.resolver import (
            UnsupportedConnectionSchemeError,
        )

        with pytest.raises(UnsupportedConnectionSchemeError):
            await resolve_connection("user/pw@host:1521/SVC", environment="production")


class TestOracleDatasourceShape:
    def test_sample_shape_accepted(self) -> None:
        from investigation_agent_platform.domain.profile.models import Datasource

        ds = Datasource(
            id="sample-orders",
            name="Sample orders database",
            kind="oracle",
            role="state",
            mcp_server="oracle-mcp-sampledb",
            read_only=True,
            tenants=["n-a"],
        )
        assert ds.kind == "oracle"
        assert ds.read_only is True

    def test_embedded_credentials_rejected(self) -> None:
        from pydantic import ValidationError

        from investigation_agent_platform.domain.profile.models import Datasource

        with pytest.raises(ValidationError):
            Datasource(
                id="bad",
                name="bad",
                kind="oracle",
                role="state",
                connection_ref="oracle://user:secret@host:1521/SVC",
            )


class TestShippedSampleEntry:
    def test_sample_entry_parses(self) -> None:
        from pathlib import Path

        from pydantic import SecretStr

        from investigation_agent_platform.bootstrap import _oracle_mcp_servers
        from investigation_agent_platform.infrastructure.configuration.config import (
            ApplicationConfig,
            BudgetConfig,
            DatabaseConfig,
            EvidenceConfig,
            LLMConfig,
        )

        repo_root = Path(__file__).resolve().parents[2]
        cfg = ApplicationConfig(
            environment="development",
            application_id="t",
            database=DatabaseConfig(connection_uri=SecretStr("x")),
            llm=LLMConfig(api_key=SecretStr("y")),
            budget=BudgetConfig(),
            evidence=EvidenceConfig(
                oracle_mcp_config=str(repo_root / "config" / "oracle-mcp-servers.yaml")
            ),
        )
        entries = {e["name"]: e for e in _oracle_mcp_servers(cfg)}
        assert "sampledb" in entries
        assert entries["sampledb"]["connection"] == "SAMPLEDB-conn"
        assert entries["sampledb"]["tenants"] == ["n-a"]
        # SQLcl 26.x speaks connect-then-query, not single-shot run-sql.
        assert entries["sampledb"]["query_tool"] == "sql_run"
        assert entries["sampledb"]["connection_tool"] == "connect"
        assert set(entries["sampledb"]["allowed_tools"]) == {"connect", "sql_run"}


class TestHyphenatedTemplateIds:
    @pytest.mark.asyncio
    async def test_sample_namespaced_id_accepted(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.security import (
            QuerySafetyPolicy,
        )

        await QuerySafetyPolicy().validate_state_request("t", "sample-orders.version", {})

    @pytest.mark.asyncio
    async def test_empty_and_injection_rejected(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import (
            SecurityPolicyViolationException,
        )
        from investigation_agent_platform.infrastructure.evidence.security import (
            QuerySafetyPolicy,
        )

        with pytest.raises(SecurityPolicyViolationException):
            await QuerySafetyPolicy().validate_state_request("t", "", {})
        with pytest.raises(SecurityPolicyViolationException):
            await QuerySafetyPolicy().validate_state_request("t", "a; DROP TABLE x", {})


class TestResolveMcpCatalog:
    def _registry(self, catalog):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.infrastructure.evidence.datasources.registry import (
            DatasourceRegistry,
        )

        return DatasourceRegistry(
            profile_repo=MagicMock(), mcp_catalog=catalog, environment="development"
        )

    def test_bare_name_resolves_with_connect_tool(self) -> None:
        from investigation_agent_platform.domain.profile.models import Datasource

        reg = self._registry({
            "sampledb": {
                "command": "docker", "args": ["run"],
                "connection": "SAMPLEDB-conn",
                "allowed_tools": ["connect", "sql_run"],
                "query_tool": "sql_run",
                "connection_tool": "connect",
                "connection_arg": "connection_name",
            }
        })
        ds = Datasource(
            id="sample-orders", name="SampleDB", kind="oracle", role="state",
            mcp_server="sampledb", read_only=True, tenants=["n-a"],
        )
        adapter = reg.resolve_mcp("n-a", MagicMock(), ds)
        assert adapter is not None
        assert adapter._query_tool == "sql_run"
        assert adapter._connection_tool == "connect"
        assert adapter._connection_arg == "connection_name"

    def test_prefixed_name_misses(self) -> None:
        from investigation_agent_platform.domain.profile.models import Datasource

        reg = self._registry({"sampledb": {"command": "docker"}})
        ds = Datasource(
            id="x", name="X", kind="oracle", role="state",
            mcp_server="oracle-mcp-sampledb", read_only=True,
        )
        assert reg.resolve_mcp("n-a", MagicMock(), ds) is None


class TestSchemaName:
    def test_accepted_and_normalized(self) -> None:
        from investigation_agent_platform.domain.profile.models import Datasource

        ds = Datasource(
            id="e", name="E", kind="oracle", role="state", schema_name="sample_owner"
        )
        assert ds.schema_name == "sample_owner"
        ds2 = Datasource(id="e", name="E", kind="oracle", role="state", schema_name="")
        assert ds2.schema_name is None

    def test_injection_rejected(self) -> None:
        from pydantic import ValidationError

        from investigation_agent_platform.domain.profile.models import Datasource

        for bad in ("x\"; DROP TABLE t; --", "a b", "1abc", "s;chema", "x" * 200):
            with pytest.raises(ValidationError):
                Datasource(id="e", name="E", kind="oracle", role="state", schema_name=bad)

    def test_resolve_schema_from_template_namespace(self) -> None:
        from unittest.mock import MagicMock

        from investigation_agent_platform.infrastructure.evidence.mcp import (
            McpEvidenceAdapter,
        )

        ds = MagicMock()
        ds.id = "sample-orders"
        ds.schema_name = "sample_owner"
        ds.query_templates = {"version": MagicMock()}
        profile = MagicMock()
        profile.datasources = [ds]
        req = MagicMock()
        req.template_id = "sample-orders.version"
        assert McpEvidenceAdapter._resolve_schema(req, profile) == "sample_owner"
        req.template_id = "other.version"
        assert McpEvidenceAdapter._resolve_schema(req, profile) == ""


class TestDialectRouting:
    def test_adapter_dialect_defaults_none(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.mcp import (
            McpEvidenceAdapter,
        )

        assert McpEvidenceAdapter(
            server_params={"command": "x", "args": []}, allowed_tools=["q"]
        ).dialect is None
        assert McpEvidenceAdapter(
            server_params={"command": "x", "args": []},
            allowed_tools=["q"], dialect="Oracle",
        ).dialect == "oracle"

    def test_query_dialect_from_template_namespace(self) -> None:
        from unittest.mock import MagicMock

        from investigation_agent_platform.application.evidence.gateway import (
            _query_dialect,
        )

        ds = MagicMock()
        ds.id = "sample-orders"
        ds.kind = MagicMock()
        ds.kind.value = "oracle"
        ds.query_templates = {"version": MagicMock()}
        profile = MagicMock()
        profile.datasources = [ds]
        assert _query_dialect(profile, "sample-orders.version") == "oracle"
        assert _query_dialect(profile, "unknown-thing") is None
        assert _query_dialect(profile, "") is None

    def test_sample_entry_declares_oracle(self) -> None:
        from pathlib import Path

        from pydantic import SecretStr

        from investigation_agent_platform.bootstrap import _oracle_mcp_servers
        from investigation_agent_platform.infrastructure.configuration.config import (
            ApplicationConfig,
            BudgetConfig,
            DatabaseConfig,
            EvidenceConfig,
            LLMConfig,
        )

        repo_root = Path(__file__).resolve().parents[2]
        cfg = ApplicationConfig(
            environment="development",
            application_id="t",
            database=DatabaseConfig(connection_uri=SecretStr("x")),
            llm=LLMConfig(api_key=SecretStr("y")),
            budget=BudgetConfig(),
            evidence=EvidenceConfig(
                oracle_mcp_config=str(repo_root / "config" / "oracle-mcp-servers.yaml")
            ),
        )
        entries = {e["name"]: e for e in _oracle_mcp_servers(cfg)}
        assert entries["sampledb"].get("dialect") == "oracle"
