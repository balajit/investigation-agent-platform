# tests/contract/test_mcp_adversarial.py
"""Part 9 Phase 6 adversarial tests: the agent is an untrusted input source.

Two enforcement tiers, both asserted here:

- **Protocol tier** (SDK validation + strict-arguments middleware): malformed
  types, out-of-range values, pattern violations, and unknown properties
  never reach a handler.
- **Envelope tier** (handlers + services): semantically invalid but
  well-typed input returns a safe ``{"error": ...}`` tool result, never a
  first-page fallback, an authorization bypass, or infrastructure internals.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from investigation_agent_platform.domain.common.exceptions import InvalidCursorException
from investigation_agent_platform.domain.evidence.completeness import EvidenceCompleteness
from investigation_agent_platform.mcp.context import (
    McpRequestContext,
    bind_mcp_context,
    reset_mcp_context,
)
from investigation_agent_platform.mcp.server import build_mcp_server

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


def _search_args(**overrides):  # type: ignore[no-untyped-def]
    args = {
        "environment": "production",
        "identifiers": None,
        "keywords": None,
        "services": None,
        "severities": None,
        "start_time": None,
        "end_time": None,
        "limit": 10,
        "cursor": None,
    }
    args.update(overrides)
    return args


async def _call(server, tool, arguments, tenant=TENANT, iid=None):  # type: ignore[no-untyped-def]
    token = bind_mcp_context(
        McpRequestContext(
            tenant_id=tenant, investigation_id=iid or uuid.uuid4(), correlation_id="c"
        )
    )
    try:
        return await server.call_tool(tool, arguments)
    finally:
        reset_mcp_context(token)


def _protocol_error(call):  # helper to assert the SDK rejected the call
    assert call.is_error, "expected a protocol-tier rejection"


@pytest.mark.asyncio
async def test_tenant_smuggling_argument_is_structurally_impossible():
    """No tenant/investigation parameter exists: smuggled values are dropped,
    and the response stays scoped to the bound transport context."""
    services = _services_with_search()
    server = build_mcp_server(services)
    iid = uuid.uuid4()
    out = await _call(
        server,
        "search_runtime_evidence",
        _search_args(tenant_id="evil-tenant", investigation_id=str(uuid.uuid4())),
        iid=iid,
    )
    assert out.is_error is False
    (scope_arg, _) = services.runtime_evidence.search.call_args.args
    assert scope_arg.tenant_id == TENANT and scope_arg.investigation_id == iid


@pytest.mark.asyncio
async def test_dsl_injection_attempts_are_plain_data():
    services = _services_with_search()
    server = build_mcp_server(services)
    payloads = [
        '{"query": {"match_all": {}}}',
        '{"term": {"labels.password": "x"}}',
        "'; DROP TABLE logs; --",
        "<script>alert(1)</script>",
        "_source.password",
    ]
    for payload in payloads:
        out = await _call(server, "search_runtime_evidence", _search_args(keywords=[payload]))
        assert out.is_error is False
    (_, request_arg) = services.runtime_evidence.search.call_args.args
    assert request_arg.keywords == [payloads[-1]]


@pytest.mark.asyncio
async def test_wildcard_and_oversized_inputs_rejected_at_protocol_tier():
    iid = uuid.uuid4()
    cases = [
        _search_args(environment="prod-*"),
        _search_args(environment="prod/x"),
        _search_args(limit=0),
        _search_args(limit=500),
        _search_args(severities=["NOPE"]),
        _search_args(severities=["error"]),
        _search_args(cursor="x" * 4097),
        _search_args(keywords=["x" * 201]),
        _search_args(keywords=["a"] * 21),
    ]
    for arguments in cases:
        # SDK type/constraint validation raises before any handler runs.
        services = _services_with_search()
        server = build_mcp_server(services)
        try:
            await _call(server, "search_runtime_evidence", arguments, iid=iid)
        except (ValidationError, MCPError, ToolError):
            pass
        else:
            pytest.fail(f"expected protocol-tier rejection for {arguments}")
        assert services.runtime_evidence.search.call_count == 0


@pytest.mark.asyncio
async def test_unauthorized_identifier_key_reaches_service_layer():
    # Part 10: structurally valid keys pass domain construction (no static
    # allowlist anymore); the provider boundary rejects unknown keys against
    # the resolved mapping. With mocked services the call succeeds here —
    # the envelope-tier rejection lives in integration (real adapter,
    # INVALID_REQUEST, zero provider I/O).
    services = _services_with_search()
    server = build_mcp_server(services)
    out = await _call(
        server, "search_runtime_evidence", _search_args(identifiers={"_source.password": "x"})
    )
    result = out.structured_content["result"]
    assert "error" not in result
    (_, request_arg) = services.runtime_evidence.search.call_args.args
    assert request_arg.identifiers == {"_source.password": "x"}


@pytest.mark.asyncio
async def test_malformed_evidence_id_rejected():
    services = _services_with_search()
    server = build_mcp_server(services)
    try:
        await _call(server, "get_evidence", {"evidence_id": "not-a-uuid"})
    except (ValidationError, MCPError, ToolError):
        pass
    else:
        pytest.fail("expected protocol-tier rejection for malformed UUID")
    assert services.evidence_detail.get.call_count == 0


@pytest.mark.asyncio
async def test_trace_input_attacks_rejected():
    iid = uuid.uuid4()
    base = {
        "trace_id": "abc123",
        "environment": "production",
        "start_time": None,
        "end_time": None,
        "max_evidence": 10,
    }
    for mutate in [
        {"trace_id": "has space"},
        {"trace_id": "x" * 201},
        {"trace_id": "../../etc"},
        {"environment": "prod-*"},
        {"max_evidence": 0},
        {"max_evidence": 500},
    ]:
        services = _services_with_search()
        server = build_mcp_server(services)
        arguments = dict(base)
        arguments.update(mutate)
        try:
            await _call(server, "investigate_trace", arguments, iid=iid)
        except (ValidationError, MCPError, ToolError):
            pass
        else:
            pytest.fail(f"expected protocol-tier rejection for {mutate}")
        assert services.trace_investigation.investigate_trace.call_count == 0


@pytest.mark.asyncio
async def test_garbage_cursor_returns_envelope_not_first_page():
    """A tampered cursor fails deterministically; the provider never runs."""
    services = _services_with_search()
    services.runtime_evidence.search = AsyncMock(side_effect=InvalidCursorException("tampered"))
    server = build_mcp_server(services)
    out = await _call(server, "search_runtime_evidence", _search_args(cursor="bogus-token"))
    result = out.structured_content["result"]
    assert result["error"]["code"] == "INVALID_CURSOR"
    assert result["error"]["retryable"] is False


@pytest.mark.asyncio
async def test_error_envelope_never_leaks_internals():
    from investigation_agent_platform.domain.common.exceptions import ExecutionError

    services = _services_with_search()
    services.runtime_evidence.search = AsyncMock(
        side_effect=ExecutionError(
            "Elasticsearch query failed: ConnectionError(hosts=['https://prod-es:9200'])"
        )
    )
    server = build_mcp_server(services)
    out = await _call(server, "search_runtime_evidence", _search_args())
    result = out.structured_content["result"]
    assert result["error"]["code"] == "INTERNAL_ERROR"
    assert "9200" not in str(result) and "prod-es" not in str(result)
