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
from investigation_agent_platform.domain.profile.models import StateProfile


def _state_profile() -> StateProfile:
    return StateProfile.model_validate({
        "provider": "oracle-mcp-tolam-stg",
        "database": "TOLAM",
        "schema": "TOLAMOWNER",
        "tables": ["ORDERS"],
        "primaryIdentifiers": ["ORDER_ID"],
        "stateFields": ["STATUS"],
        "timestampFields": ["UPDATED_AT"],
        "queryTemplates": {},
    })


def _request(sql: str) -> ApplicationStateRequest:
    return ApplicationStateRequest(environment="staging", template_id="adhoc_select",
                                   parameters={"sql": sql}, limit=10)


class TestSelectOnlyGate:
    @pytest.mark.asyncio
    async def test_accepts_select_and_with(self) -> None:
        from investigation_agent_platform.infrastructure.evidence.security import (
            QuerySafetyPolicy,
        )

        policy = QuerySafetyPolicy()
        out = await policy.validate_generated_sql("t", "SELECT a FROM t WHERE x = 1")
        assert "SELECT" in out.upper()
        out = await policy.validate_generated_sql("t", "WITH x AS (SELECT 1 FROM dual) SELECT * FROM x")
        assert "SELECT" in out.upper()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "DROP TABLE t",
        "CREATE TABLE x (a INT)",
        "BEGIN DBMS_OUTPUT.PUT_LINE('x'); END;",
        "SELECT 1; DELETE FROM t",
        "",
        "not sql at all ((((",
    ])
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
        assert arguments["connection"] == "TOLAM-stg"
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
            "connection_name": "TOLAM-stg",
            "tenant_allowlist": {"tenant-a": {"TOLAM-stg"}},
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
        with patch("mcp.client.stdio.stdio_client", return_value=stdio), patch(
            "mcp.ClientSession", return_value=client_session
        ):
            result = await adapter.search_application_state(
                "tenant-a", UUID(int=1), _request("SELECT * FROM orders WHERE id = 123"),
                _state_profile(),
            )
        assert result.total_count == 2

    @pytest.mark.asyncio
    async def test_dml_rejected_before_dispatch(self) -> None:
        adapter = self._adapter()
        with patch("mcp.client.stdio.stdio_client") as stdio:
            with pytest.raises(SecurityPolicyViolationException):
                await adapter.search_application_state(
                    "tenant-a", UUID(int=1), _request("DELETE FROM orders"),
                    _state_profile(),
                )
            stdio.assert_not_called()

    @pytest.mark.asyncio
    async def test_tenant_denied(self) -> None:
        adapter = self._adapter()
        with pytest.raises(SecurityPolicyViolationException, match="may not use"):
            await adapter.search_application_state(
                "tenant-evil", UUID(int=1), _request("SELECT 1 FROM dual"),
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
            environment="production", application_id="t",
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
            environment="production", application_id="t",
            database=DatabaseConfig(connection_uri=SecretStr("x")),
            llm=LLMConfig(api_key=SecretStr("y")),
            budget=BudgetConfig(),
            evidence=EvidenceConfig(oracle_mcp_config=str(path)),
        )
        assert [e["name"] for e in _oracle_mcp_servers(cfg)] == ["good"]
