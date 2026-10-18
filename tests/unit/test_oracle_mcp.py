# tests/unit/test_oracle_mcp.py
"""Oracle SQLcl MCP registration + adapter tests (Part 3 Oracle/MCP addendum).

Covers: SELECT-only gate (accept/with/select, reject DML/DDL/PLSQL/garbage),
adapter run-sql dispatch with mocked stdio session, tenant allowlist
default-deny, protocol parity (state provider check passes), and YAML
registration rules (missing file/incomplete/no-tenants).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from investigation_agent_platform.domain.common.exceptions import SecurityPolicyViolationException
from investigation_agent_platform.domain.evidence.requests import ApplicationStateRequest
from investigation_agent_platform.domain.profile.models import ApplicationProfile


def _state_profile() -> ApplicationProfile:
    return ApplicationProfile(
        id="app",
        tenant_id="tenant-a",
        name="n",
        description="d",
        environment="staging",
    )


def _request(sql: str) -> ApplicationStateRequest:
    return ApplicationStateRequest(
        environment="staging", template_id="adhoc_select", parameters={"sql": sql}, limit=10
    )


class TestSelectOnlyGate:
    @pytest.mark.asyncio
    async def test_accepts_select_and_with(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.security import (
            QuerySafetyPolicy,
        )

        policy = QuerySafetyPolicy()
        out = await policy.validate_generated_sql("t", "SELECT a FROM t WHERE x = 1")
        assert "SELECT" in out.upper()
        out = await policy.validate_generated_sql(
            "t", "WITH x AS (SELECT 1 FROM dual) SELECT * FROM x"
        )
        assert "SELECT" in out.upper()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad",
        [
            "INSERT INTO t VALUES (1)",
            "UPDATE t SET a = 1",
            "DELETE FROM t",
            "DROP TABLE t",
            "CREATE TABLE x (a INT)",
            "BEGIN DBMS_OUTPUT.PUT_LINE('x'); END;",
            "SELECT 1; DELETE FROM t",
            "",
            "not sql at all ((((",
        ],
    )
    async def test_rejects_non_select(self, bad: str) -> None:
        from investigation_agent_platform.infrastructure.evidence.security import (
            QuerySafetyPolicy,
        )

        with pytest.raises(SecurityPolicyViolationException):
            await QuerySafetyPolicy().validate_generated_sql("t", bad)

    @pytest.mark.asyncio
    async def test_rejects_oversized(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.security import (
            QuerySafetyPolicy,
        )

        with pytest.raises(SecurityPolicyViolationException):
            await QuerySafetyPolicy().validate_generated_sql("t", "SELECT 1" + " " * 9000, 100)


def _session_mock(rows: list[str]):  # type: ignore[no-untyped-def]
    content = [MagicMock(text=r) for r in rows]

    async def _call_tool(name, arguments=None):  # type: ignore[no-untyped-def]
        assert name == "run-sql"
        assert arguments["connection"] == "EXAMPLEDB-conn"
        assert "SELECT" in arguments["sql"].upper()
        assert arguments["max_rows"] <= 10
        return MagicMock(content=content)

    session = MagicMock()
    session.initialize = AsyncMock(return_value=None)
    session.call_tool = AsyncMock(side_effect=_call_tool)
    return session


class TestMcpStateQuery:
    def _adapter(self, **over):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.infrastructure.evidence.mcp import (
            McpEvidenceAdapter,
        )

        kwargs = {
            "server_params": {"command": "docker", "args": ["run"]},
            "allowed_tools": ["run-sql"],
            "connection_name": "EXAMPLEDB-conn",
            "tenant_allowlist": {"tenant-a": {"EXAMPLEDB-conn"}},
        }
        kwargs.update(over)
        return McpEvidenceAdapter(**kwargs)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_run_sql_dispatch(self) -> None:
        adapter = self._adapter()
        session = _session_mock(["ORDER_ID=123", "STATUS=SHIPPED"])
        stdio = MagicMock()
        stdio.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
        stdio.__aexit__ = AsyncMock(return_value=False)
        client_session = MagicMock()
        client_session.__aenter__ = AsyncMock(return_value=session)
        client_session.__aexit__ = AsyncMock(return_value=False)
        with (
            patch("mcp.client.stdio.stdio_client", return_value=stdio),
            patch("mcp.ClientSession", return_value=client_session),
        ):
            result = await adapter.search_application_state(
                "tenant-a",
                UUID(int=1),
                _request("SELECT * FROM orders WHERE id = 123"),
                _state_profile(),
            )
        assert result.total_count == 2

    @pytest.mark.asyncio
    async def test_dml_rejected_before_dispatch(self) -> None:
        adapter = self._adapter()
        with patch("mcp.client.stdio.stdio_client") as stdio:
            with pytest.raises(SecurityPolicyViolationException):
                await adapter.search_application_state(
                    "tenant-a",
                    UUID(int=1),
                    _request("DELETE FROM orders"),
                    _state_profile(),
                )
            stdio.assert_not_called()

    @pytest.mark.asyncio
    async def test_tenant_denied(self) -> None:
        adapter = self._adapter()
        with pytest.raises(SecurityPolicyViolationException, match="may not use"):
            await adapter.search_application_state(
                "tenant-evil",
                UUID(int=1),
                _request("SELECT 1 FROM dual"),
                _state_profile(),
            )

    def test_state_protocol_parity(self) -> None:
        from investigation_agent_platform.ports.evidence.state import (
            StateEvidenceProviderProtocol,
        )

        assert isinstance(self._adapter(), StateEvidenceProviderProtocol)


class TestMcpRegistration:
    def test_missing_file_registers_nothing(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from pydantic import SecretStr

        from investigation_agent_platform.bootstrap import _oracle_mcp_servers
        from investigation_agent_platform.infrastructure.configuration.config import (
            ApplicationConfig,
            BudgetConfig,
            DatabaseConfig,
            EvidenceConfig,
            LLMConfig,
        )

        cfg = ApplicationConfig(
            environment="production",
            application_id="t",
            database=DatabaseConfig(connection_uri=SecretStr("x")),
            llm=LLMConfig(api_key=SecretStr("y")),
            budget=BudgetConfig(),
            evidence=EvidenceConfig(oracle_mcp_config=str(tmp_path / "nope.yaml")),
        )
        assert _oracle_mcp_servers(cfg) == []

    def test_incomplete_and_tenantless_skipped(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from pydantic import SecretStr

        from investigation_agent_platform.bootstrap import _oracle_mcp_servers
        from investigation_agent_platform.infrastructure.configuration.config import (
            ApplicationConfig,
            BudgetConfig,
            DatabaseConfig,
            EvidenceConfig,
            LLMConfig,
        )

        path = tmp_path / "mcp.yaml"
        path.write_text(
            "servers:\n"
            "  - {name: bad, command: docker}\n"
            "  - {name: lonely, command: docker, args: [x], connection: C, tenants: []}\n"
            "  - {name: good, command: docker, args: [x], connection: C, tenants: [t]}\n"
        )
        cfg = ApplicationConfig(
            environment="production",
            application_id="t",
            database=DatabaseConfig(connection_uri=SecretStr("x")),
            llm=LLMConfig(api_key=SecretStr("y")),
            budget=BudgetConfig(),
            evidence=EvidenceConfig(oracle_mcp_config=str(path)),
        )
        assert [e["name"] for e in _oracle_mcp_servers(cfg)] == ["good"]


def _connect_session_mock(calls: list, rows: list[str]):  # type: ignore[no-untyped-def]
    content = [MagicMock(text=r) for r in rows]

    async def _call_tool(name, arguments=None):  # type: ignore[no-untyped-def]
        calls.append((name, dict(arguments or {})))
        if name == "connect":
            assert arguments["connection_name"] == "EXAMPLEDB-conn"
            return MagicMock(content=[MagicMock(text="Connected")])
        assert name == "sql_run"
        assert "SELECT" in arguments["sql"].upper()
        assert "connection" not in arguments
        return MagicMock(content=content)

    session = MagicMock()
    session.initialize = AsyncMock(return_value=None)
    session.call_tool = AsyncMock(side_effect=_call_tool)
    return session


def _patched_session(session):  # type: ignore[no-untyped-def]
    from unittest.mock import patch

    stdio = MagicMock()
    stdio.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
    stdio.__aexit__ = AsyncMock(return_value=False)
    client_session = MagicMock()
    client_session.__aenter__ = AsyncMock(return_value=session)
    client_session.__aexit__ = AsyncMock(return_value=False)
    return (
        patch("mcp.client.stdio.stdio_client", return_value=stdio),
        patch("mcp.ClientSession", return_value=client_session),
    )


class TestConnectThenQuery:
    def _adapter(self, **over):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.infrastructure.evidence.mcp import (
            McpEvidenceAdapter,
        )

        kwargs = {
            "server_params": {"command": "docker", "args": ["run"]},
            "allowed_tools": ["connect", "sql_run"],
            "connection_name": "EXAMPLEDB-conn",
            "tenant_allowlist": {"tenant-a": {"EXAMPLEDB-conn"}},
            "query_tool": "sql_run",
            "connection_tool": "connect",
            "connection_arg": "connection_name",
        }
        kwargs.update(over)
        return McpEvidenceAdapter(**kwargs)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_connect_before_query(self) -> None:
        from uuid import UUID

        adapter = self._adapter()
        calls: list = []
        session = _connect_session_mock(calls, ["BANNER=Oracle Database 19c"])
        p1, p2 = _patched_session(session)
        with p1, p2:
            result = await adapter.search_application_state(
                "tenant-a",
                UUID(int=1),
                _request("SELECT * FROM v$version"),
                _state_profile(),
            )
        assert [c[0] for c in calls] == ["connect", "sql_run"]
        assert result.total_count == 1
        # #65: state rows bank as DATABASE_STATE with connection provenance.
        from investigation_agent_platform.domain.evidence.models import EvidenceType

        row = result.items[0]
        assert row.evidence_type == EvidenceType.DATABASE_STATE
        assert "EXAMPLEDB-conn" in (row.source or "")
        assert "EXAMPLEDB-conn" in (row.provider or "")

    @pytest.mark.asyncio
    async def test_disallowed_connect_tool_refused(self) -> None:
        from uuid import UUID

        from investigation_agent_platform.domain.common.exceptions import (
            SecurityPolicyViolationException,
        )

        adapter = self._adapter(allowed_tools=["sql_run"])
        with pytest.raises(SecurityPolicyViolationException):
            await adapter.search_application_state(
                "tenant-a",
                UUID(int=1),
                _request("SELECT 1 FROM dual"),
                _state_profile(),
            )

    @pytest.mark.asyncio
    async def test_connect_failure_surfaces(self) -> None:
        from uuid import UUID

        from investigation_agent_platform.domain.common.exceptions import ExecutionError

        adapter = self._adapter()
        session = MagicMock()
        session.initialize = AsyncMock(return_value=None)

        async def _fail(name, arguments=None):  # type: ignore[no-untyped-def]
            if name == "connect":
                raise RuntimeError("ORA-12170: connect timeout")
            return MagicMock(content=[])

        session.call_tool = AsyncMock(side_effect=_fail)
        p1, p2 = _patched_session(session)
        with p1, p2:
            with pytest.raises(ExecutionError, match="connect"):
                await adapter.search_application_state(
                    "tenant-a",
                    UUID(int=1),
                    _request("SELECT 1 FROM dual"),
                    _state_profile(),
                )


class TestServerParamsNormalization:
    def test_dict_becomes_stdio_params(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.mcp import (
            McpEvidenceAdapter,
            _normalize_server_params,
        )

        normalized = _normalize_server_params({"command": "docker", "args": ["run"]})
        assert not isinstance(normalized, dict)
        assert normalized.command == "docker"
        assert normalized.args == ["run"]
        adapter = McpEvidenceAdapter(
            server_params={"command": "docker", "args": ["run"]},
            allowed_tools=["connect", "sql_run"],
        )
        assert adapter._server_params.command == "docker"


class TestServerArgsExpansion:
    def test_compose_style_mounts_expand(self, monkeypatch) -> None:
        from investigation_agent_platform.infrastructure.evidence.mcp import (
            _expand_env_refs,
        )

        monkeypatch.delenv("IAP_ORACLE_MCP_CONNECTIONS_DIR", raising=False)
        monkeypatch.setenv("HOME", "/Users/tester")
        out = _expand_env_refs(
            "${IAP_ORACLE_MCP_CONNECTIONS_DIR:-${HOME}/workspace/docker/iap/oracle-mcp/connections}:/config:ro"
        )
        assert out == "/Users/tester/workspace/docker/iap/oracle-mcp/connections:/config:ro"

    def test_explicit_override_wins(self, monkeypatch) -> None:
        from investigation_agent_platform.infrastructure.evidence.mcp import (
            _expand_env_refs,
        )

        monkeypatch.setenv("IAP_ORACLE_MCP_CONNECTIONS_DIR", "/data/conns")
        out = _expand_env_refs("${IAP_ORACLE_MCP_CONNECTIONS_DIR:-/default}:/config:ro")
        assert out == "/data/conns:/config:ro"

    def test_unset_without_default_stays_literal(self, monkeypatch) -> None:
        from investigation_agent_platform.infrastructure.evidence.mcp import (
            _expand_env_refs,
        )

        monkeypatch.delenv("IAP_DEFINITELY_UNSET_VAR_XYZ", raising=False)
        assert _expand_env_refs("${IAP_DEFINITELY_UNSET_VAR_XYZ}") == "${IAP_DEFINITELY_UNSET_VAR_XYZ}"


class TestCurrentSchema:
    def _schema_adapter(self, **over):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.infrastructure.evidence.mcp import (
            McpEvidenceAdapter,
        )

        kwargs = {
            "server_params": {"command": "docker", "args": ["run"]},
            "allowed_tools": ["connect", "sql_run"],
            "connection_name": "EXAMPLEDB-conn",
            "tenant_allowlist": {"tenant-a": {"EXAMPLEDB-conn"}},
            "query_tool": "sql_run",
            "connection_tool": "connect",
            "connection_arg": "connection_name",
        }
        kwargs.update(over)
        return McpEvidenceAdapter(**kwargs)  # type: ignore[arg-type]

    def _schema_profile(self):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.domain.profile.models import (
            Datasource,
            QueryTemplate,
        )

        profile = _state_profile()
        ds = Datasource(
            id="sample-orders",
            name="SampleDB",
            kind="oracle",
            role="state",
            mcp_server="sampledb",
            read_only=True,
            schema_name="sample_owner",
            query_templates={
                "version": QueryTemplate(
                    description="v", sql="SELECT BANNER FROM v$version"
                )
            },
        )
        return profile.model_copy(update={"datasources": [ds]})

    @pytest.mark.asyncio
    async def test_alter_between_connect_and_query(self) -> None:
        from uuid import UUID

        from investigation_agent_platform.domain.evidence.requests import (
            ApplicationStateRequest,
        )

        adapter = self._schema_adapter()
        calls: list = []
        content = [MagicMock(text="BANNER=X")]

        async def _call_tool(name, arguments=None):  # type: ignore[no-untyped-def]
            calls.append((name, dict(arguments or {})))
            return MagicMock(content=content if name == "sql_run" else [MagicMock(text="ok")])

        session = MagicMock()
        session.initialize = AsyncMock(return_value=None)
        session.call_tool = AsyncMock(side_effect=_call_tool)
        p1, p2 = _patched_session(session)
        req = ApplicationStateRequest(
            environment="development",
            template_id="sample-orders.version",
            parameters={"template_id": "sample-orders.version"},
            limit=10,
        )
        with p1, p2:
            result = await adapter.search_application_state(
                "tenant-a", UUID(int=1), req, self._schema_profile()
            )
        assert [c[0] for c in calls] == ["connect", "sql_run", "sql_run"]
        assert calls[1][1]["sql"] == "ALTER SESSION SET CURRENT_SCHEMA = sample_owner"
        assert "BANNER" in calls[2][1]["sql"]
        assert result.total_count == 1

    @pytest.mark.asyncio
    async def test_no_schema_no_alter(self) -> None:
        from uuid import UUID

        adapter = self._schema_adapter()
        calls: list = []

        async def _call_tool(name, arguments=None):  # type: ignore[no-untyped-def]
            calls.append(name)
            return MagicMock(content=[MagicMock(text="x")])

        session = MagicMock()
        session.initialize = AsyncMock(return_value=None)
        session.call_tool = AsyncMock(side_effect=_call_tool)
        p1, p2 = _patched_session(session)
        with p1, p2:
            await adapter.search_application_state(
                "tenant-a", UUID(int=1),
                _request("SELECT 1 FROM dual"), _state_profile(),
            )
        assert calls == ["connect", "sql_run"]
