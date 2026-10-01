# tests/unit/test_mcp_tools.py
"""Part 9 Phase 6 unit tests: tool cores, error mapping, context, registry, middleware."""

import uuid
from datetime import UTC, datetime
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from investigation_agent_platform.application.investigation.context import InvestigationScope
from investigation_agent_platform.domain.common.exceptions import (
    EvidenceNotFoundException,
    ExecutionError,
    InvalidCursorException,
    ProviderTimeoutException,
    ProviderUnavailableException,
    SecurityPolicyViolationException,
)
from investigation_agent_platform.domain.evidence.completeness import EvidenceCompleteness
from investigation_agent_platform.mcp.context import (
    McpRequestContext,
    bind_mcp_context,
    require_mcp_context,
    reset_mcp_context,
    sanitize_correlation_id,
    stdio_context_from_env,
)
from investigation_agent_platform.mcp.errors import (
    McpErrorCode,
    McpErrorEnvelope,
    error_envelope,
    map_exception_to_error,
)
from investigation_agent_platform.mcp.registry import EXPECTED_TOOLS, TOOL_ARGUMENT_PROPERTIES

TENANT = "tenant-a"


def _scope(iid=None):  # type: ignore[no-untyped-def]
    return InvestigationScope.create(tenant_id=TENANT, investigation_id=iid or uuid.uuid4())


def _search_result(items=None, has_more=False, cursor=None):  # type: ignore[no-untyped-def]
    result = MagicMock()
    result.items = items or []
    result.next_cursor = cursor
    result.has_more = has_more
    result.total_count = len(items or [])
    result.completeness = EvidenceCompleteness(
        complete=not has_more,
        reason="pagination_available" if has_more else None,
        returned_count=len(items or []),
        examined_count=len(items or []),
    )
    return result


def _services(search_result=None, evidence=None, telemetry=None):  # type: ignore[no-untyped-def]
    services = MagicMock()
    services.runtime_evidence.search = AsyncMock(return_value=search_result or _search_result())
    services.evidence_detail.get = AsyncMock(return_value=evidence)
    services.trace_investigation.investigate_trace = AsyncMock(return_value=telemetry)
    return services


# --- Error mapping ---------------------------------------------------------------------


def test_error_mapping_covers_taxonomy():
    assert map_exception_to_error(InvalidCursorException("x")).code == McpErrorCode.INVALID_CURSOR
    assert (
        map_exception_to_error(EvidenceNotFoundException("x")).code
        == McpErrorCode.EVIDENCE_NOT_FOUND
    )
    assert (
        map_exception_to_error(SecurityPolicyViolationException("x")).code == McpErrorCode.FORBIDDEN
    )
    timeout = map_exception_to_error(ProviderTimeoutException("x"))
    assert timeout.code == McpErrorCode.PROVIDER_TIMEOUT and timeout.retryable is True
    unavailable = map_exception_to_error(ProviderUnavailableException("x"))
    assert unavailable.code == McpErrorCode.PROVIDER_UNAVAILABLE and unavailable.retryable is True
    internal = map_exception_to_error(ExecutionError("boom"))
    assert internal.code == McpErrorCode.INTERNAL_ERROR
    assert "boom" not in internal.message
    assert map_exception_to_error(ValueError("weird")).code == McpErrorCode.INTERNAL_ERROR
    assert (
        map_exception_to_error(ValueError("weird")).message
        == "An internal investigation failure occurred."
    )


def test_error_envelope_shape_and_message_cap():
    envelope = error_envelope(ExecutionError("x" * 5000))
    assert isinstance(envelope, McpErrorEnvelope)
    assert len(envelope.error.message) <= 1000


def test_all_error_codes_enumerated():
    assert {code.value for code in McpErrorCode} == {
        "INVALID_REQUEST",
        "INVALID_CURSOR",
        "CURSOR_EXPIRED",
        "FORBIDDEN",
        "EVIDENCE_NOT_FOUND",
        "PROVIDER_TIMEOUT",
        "PROVIDER_UNAVAILABLE",
        "INCOMPLETE_EVIDENCE",
        "INTERNAL_ERROR",
    }


# --- Context -----------------------------------------------------------------------------


def test_require_context_fails_closed_without_binding():
    with pytest.raises(RuntimeError):
        require_mcp_context()


def test_bind_require_reset_cycle():
    context = McpRequestContext(tenant_id=TENANT, investigation_id=uuid.uuid4(), correlation_id="c")
    token = bind_mcp_context(context)
    try:
        assert require_mcp_context() is context
        scope = context.to_scope()
        assert scope.tenant_id == TENANT and scope.correlation_id == "c"
    finally:
        reset_mcp_context(token)
    with pytest.raises(RuntimeError):
        require_mcp_context()


def test_sanitize_correlation_id():
    assert sanitize_correlation_id("abc-123:X") == "abc-123:X"
    assert sanitize_correlation_id("has space") != "has space"
    assert sanitize_correlation_id(None) != ""


def test_stdio_context_requires_configuration():
    with pytest.raises(RuntimeError):
        stdio_context_from_env("", "", None)
    with pytest.raises(RuntimeError):
        stdio_context_from_env("t", "not-a-uuid", None)
    context = stdio_context_from_env("t", str(uuid.uuid4()), "op")
    assert context.actor_id == "op"


# --- Registry -------------------------------------------------------------------------------


def test_expected_tool_catalog():
    assert EXPECTED_TOOLS == {
        "search_runtime_evidence",
        "get_evidence",
        "investigate_trace",
        "search_reference_docs",
    }


def test_tool_argument_surface_has_no_auth_or_infra_params():
    forbidden = {
        "tenant_id",
        "tenant",
        "investigation_id",
        "authorization",
        "index",
        "query",
        "body",
        "dsl",
        "sort",
        "aggregation",
        "aggregations",
        "script",
    }
    for tool, properties in TOOL_ARGUMENT_PROPERTIES.items():
        assert not (set(properties) & forbidden), tool
    assert set(TOOL_ARGUMENT_PROPERTIES) == EXPECTED_TOOLS


# --- Tool cores -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_core_success_and_pagination():
    from investigation_agent_platform.mcp.schemas.evidence import RuntimeEvidenceSearchOutput
    from investigation_agent_platform.mcp.tools.runtime_evidence import (
        search_runtime_evidence_core,
    )

    services = _services(_search_result(has_more=True, cursor="tok"))
    output = await search_runtime_evidence_core(
        services=services,
        scope=_scope(),
        environment="production",
        identifiers={"trace_id": "abc"},
        keywords=["timeout"],
        service_names=["checkout"],
        severities=[],
        start_time=None,
        end_time=None,
        limit=25,
        cursor=None,
    )
    assert isinstance(output, RuntimeEvidenceSearchOutput)
    assert output.has_more is True and output.next_cursor == "tok"
    assert output.completeness.reason == "pagination_available"
    # Trusted scope reaches the service (tenant/investigation asserted there).
    (scope_arg, request_arg) = services.runtime_evidence.search.call_args.args
    assert scope_arg.tenant_id == TENANT
    assert request_arg.environment == "production"


@pytest.mark.asyncio
async def test_search_core_partial_time_range_rejected():
    from investigation_agent_platform.mcp.errors import McpErrorEnvelope
    from investigation_agent_platform.mcp.tools.runtime_evidence import (
        search_runtime_evidence_core,
    )

    output = await search_runtime_evidence_core(
        services=_services(),
        scope=_scope(),
        environment="production",
        identifiers=None,
        keywords=None,
        service_names=None,
        severities=None,
        start_time=datetime.now(UTC),
        end_time=None,
        limit=10,
        cursor=None,
    )
    assert isinstance(output, McpErrorEnvelope)
    assert output.error.code == McpErrorCode.INVALID_REQUEST


@pytest.mark.asyncio
async def test_search_core_maps_domain_failures():
    from investigation_agent_platform.mcp.errors import McpErrorEnvelope
    from investigation_agent_platform.mcp.tools.runtime_evidence import (
        search_runtime_evidence_core,
    )

    services = _services()
    services.runtime_evidence.search = AsyncMock(side_effect=InvalidCursorException("bad"))
    output = await search_runtime_evidence_core(
        services=services,
        scope=_scope(),
        environment="production",
        identifiers=None,
        keywords=None,
        service_names=None,
        severities=None,
        start_time=None,
        end_time=None,
        limit=10,
        cursor="bogus",
    )
    assert isinstance(output, McpErrorEnvelope)
    assert output.error.code == McpErrorCode.INVALID_CURSOR
    assert output.error.retryable is False


@pytest.mark.asyncio
async def test_search_core_rejects_invalid_severity_at_domain():
    from investigation_agent_platform.mcp.errors import McpErrorEnvelope
    from investigation_agent_platform.mcp.tools.runtime_evidence import (
        search_runtime_evidence_core,
    )

    output = await search_runtime_evidence_core(
        services=_services(),
        scope=_scope(),
        environment="production",
        identifiers=None,
        keywords=None,
        service_names=None,
        severities=["NOPE"],  # type: ignore[list-item]
        start_time=None,
        end_time=None,
        limit=10,
        cursor=None,
    )
    assert isinstance(output, McpErrorEnvelope)
    assert output.error.code == McpErrorCode.INVALID_REQUEST


def _make_evidence(iid, eid=None):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
    from investigation_agent_platform.domain.provenance.models import (
        EvidenceFreshness,
        EvidenceProvenance,
        QueryFingerprint,
        SourceLocation,
    )

    now = datetime.now(UTC)
    return Evidence(
        tenant_id=TENANT,
        investigation_id=iid,
        evidence_id=eid or uuid.uuid4(),
        evidence_type=EvidenceType.RUNTIME_LOG,
        provider="ELASTIC",
        source="elasticsearch://idx/doc",
        title="t",
        summary="s",
        fingerprint="fp",
        observed_at=now,
        retrieved_at=now,
        attributes={"message": "m"},
        provenance=EvidenceProvenance(
            tenant_id=TENANT,
            investigation_id=iid,
            provider_type="ELASTIC",
            requested_provider_id="elastic-primary",
            actual_provider_id="elastic-primary",
            source_system="Elasticsearch",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="ELASTIC", operation="SEARCH", normalized_query_hash="f" * 64
            ),
            source_location=SourceLocation(system="Elasticsearch", identifier="idx/doc"),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
    )


@pytest.mark.asyncio
async def test_get_core_success_and_not_found():
    from investigation_agent_platform.mcp.errors import McpErrorEnvelope
    from investigation_agent_platform.mcp.schemas.evidence import EvidenceDetailOutput
    from investigation_agent_platform.mcp.tools.evidence_detail import get_evidence_core

    iid = uuid.uuid4()
    eid = uuid.uuid4()
    services = _services(evidence=_make_evidence(iid, eid))
    output = await get_evidence_core(services=services, scope=_scope(iid), evidence_id=eid)
    assert isinstance(output, EvidenceDetailOutput)
    assert output.evidence_id == eid
    assert output.provenance.provider == "ELASTIC"

    services2 = _services()
    services2.evidence_detail.get = AsyncMock(side_effect=EvidenceNotFoundException("nope"))
    output2 = await get_evidence_core(services=services2, scope=_scope(iid), evidence_id=eid)
    assert isinstance(output2, McpErrorEnvelope)
    assert output2.error.code == McpErrorCode.EVIDENCE_NOT_FOUND


@pytest.mark.asyncio
async def test_trace_core_success_and_failure():
    from investigation_agent_platform.domain.evidence.telemetry import TraceTelemetry
    from investigation_agent_platform.mcp.errors import McpErrorEnvelope
    from investigation_agent_platform.mcp.schemas.trace import InvestigateTraceOutput
    from investigation_agent_platform.mcp.tools.trace_investigation import (
        investigate_trace_core,
    )

    telemetry = TraceTelemetry(
        trace_id="abc",
        completeness={"complete": True, "returned_count": 0, "examined_count": 0},  # type: ignore[arg-type]
    )
    result = MagicMock()
    result.telemetry = telemetry
    result.evidence = []
    output = await investigate_trace_core(
        services=_services(telemetry=result),
        scope=_scope(),
        trace_id="abc",
        environment="production",
        start_time=None,
        end_time=None,
        max_evidence=50,
    )
    assert isinstance(output, InvestigateTraceOutput)
    assert output.trace_id == "abc" and output.completeness.complete is True

    services2 = _services()
    services2.trace_investigation.investigate_trace = AsyncMock(
        side_effect=ProviderTimeoutException("slow")
    )
    output2 = await investigate_trace_core(
        services=services2,
        scope=_scope(),
        trace_id="abc",
        environment="production",
        start_time=None,
        end_time=None,
        max_evidence=50,
    )
    assert isinstance(output2, McpErrorEnvelope)
    assert output2.error.code == McpErrorCode.PROVIDER_TIMEOUT
    assert output2.error.retryable is True


# --- Strict arguments middleware ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strict_middleware_rejects_unknown_properties():
    from mcp.shared.exceptions import MCPError

    from investigation_agent_platform.mcp.middleware import StrictToolArgumentsMiddleware
    from investigation_agent_platform.mcp.registry import TOOL_ARGUMENT_PROPERTIES

    middleware = StrictToolArgumentsMiddleware(TOOL_ARGUMENT_PROPERTIES)
    calls = []

    class _Ctx:
        method = "tools/call"
        params: ClassVar[dict] = {
            "name": "search_runtime_evidence",
            "arguments": {"environment": "p", "index": "x"},
        }

    async def _next(ctx):  # type: ignore[no-untyped-def]
        calls.append(True)
        return {}

    with pytest.raises(MCPError) as caught:
        await middleware(_Ctx(), _next)  # type: ignore[arg-type]
    assert caught.value.code == -32602
    assert calls == []


@pytest.mark.asyncio
async def test_strict_middleware_passes_known_properties():
    from investigation_agent_platform.mcp.middleware import StrictToolArgumentsMiddleware
    from investigation_agent_platform.mcp.registry import TOOL_ARGUMENT_PROPERTIES

    middleware = StrictToolArgumentsMiddleware(TOOL_ARGUMENT_PROPERTIES)

    class _Ctx:
        method = "tools/call"
        params: ClassVar[dict] = {"name": "get_evidence", "arguments": {"evidence_id": "x"}}

    async def _next(ctx):  # type: ignore[no-untyped-def]
        return {"ok": True}

    assert await middleware(_Ctx(), _next) == {"ok": True}  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_strict_middleware_ignores_other_methods():
    from investigation_agent_platform.mcp.middleware import StrictToolArgumentsMiddleware

    middleware = StrictToolArgumentsMiddleware({})

    class _Ctx:
        method = "initialize"
        params: ClassVar[dict] = {}

    async def _next(ctx):  # type: ignore[no-untyped-def]
        return {"ok": True}

    assert await middleware(_Ctx(), _next) == {"ok": True}  # type: ignore[arg-type]
