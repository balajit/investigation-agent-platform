# src/investigation_agent_platform/mcp/tools/runtime_evidence.py
"""MCP ``search_runtime_evidence`` tool (Part 9 Phase 6).

Thin agent-facing adapter: SDK-derived input validation, trusted context
from the transport (never tool arguments), domain request construction, one
application-service call, domain-to-MCP projection. No Elastic DSL, no index
names, no authorization inputs exist in this contract — unsafe operations
are structurally inexpressible, not merely validated away.
"""

import logging
from datetime import datetime
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from opentelemetry import trace
from pydantic import Field

from investigation_agent_platform.application.investigation.composition import (
    InvestigationServices,
)
from investigation_agent_platform.application.investigation.context import InvestigationScope
from investigation_agent_platform.domain.common.exceptions import DomainValidationException
from investigation_agent_platform.domain.evidence.models import LogSeverity
from investigation_agent_platform.domain.evidence.requests import (
    ENVIRONMENT_PATTERN,
    MAX_IDENTIFIER_VALUE_LENGTH,
    MAX_KEYWORD_LENGTH,
    MAX_SERVICE_LENGTH,
    RuntimeEvidenceRequest,
    TimeRange,
)
from investigation_agent_platform.mcp.context import require_mcp_context
from investigation_agent_platform.mcp.errors import McpErrorEnvelope, error_envelope
from investigation_agent_platform.mcp.schemas.evidence import RuntimeEvidenceSearchOutput

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

TOOL_NAME = "search_runtime_evidence"
TOOL_TITLE = "Search Runtime Evidence"
TOOL_DESCRIPTION = (
    "Search bounded runtime evidence within the current authenticated investigation. "
    "Results are automatically scoped to the authorized tenant and investigation. "
    "Use the returned next_cursor only with the same search criteria. "
    "This tool does not accept raw Elasticsearch queries, index names, or authorization context."
)
TOOL_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)

EnvironmentParam = Annotated[str, Field(min_length=1, max_length=100, pattern=ENVIRONMENT_PATTERN)]
IdentifiersParam = Annotated[
    dict[str, Annotated[str, Field(min_length=1, max_length=MAX_IDENTIFIER_VALUE_LENGTH)]] | None,
    Field(max_length=10),
]
KeywordsParam = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=MAX_KEYWORD_LENGTH)]] | None,
    Field(max_length=20),
]
ServicesParam = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=MAX_SERVICE_LENGTH)]] | None,
    Field(max_length=20),
]
SeveritiesParam = Annotated[list[LogSeverity] | None, Field(max_length=10)]
LimitParam = Annotated[int, Field(ge=1, le=100)]
CursorParam = Annotated[str | None, Field(min_length=1, max_length=4096)]


async def search_runtime_evidence_core(
    *,
    services: InvestigationServices,
    scope: InvestigationScope,
    environment: str,
    identifiers: dict[str, str] | None,
    keywords: list[str] | None,
    service_names: list[str] | None,
    severities: list[LogSeverity] | None,
    start_time: datetime | None,
    end_time: datetime | None,
    limit: int,
    cursor: str | None,
) -> RuntimeEvidenceSearchOutput | McpErrorEnvelope:
    """Execute the search; every failure becomes a safe error envelope."""
    with tracer.start_as_current_span("mcp.search_runtime_evidence") as span:
        span.set_attribute("tool", TOOL_NAME)
        span.set_attribute("tenant_id", scope.tenant_id)
        span.set_attribute("investigation_id", str(scope.investigation_id))
        span.set_attribute("correlation_id", scope.correlation_id)
        span.set_attribute("environment", environment)
        try:
            time_range: TimeRange | None = None
            if start_time is not None or end_time is not None:
                if start_time is None or end_time is None:
                    raise DomainValidationException(
                        "Provide both start_time and end_time, or neither."
                    )
                time_range = TimeRange(start_time=start_time, end_time=end_time)
            request = RuntimeEvidenceRequest(
                environment=environment,
                identifiers=dict(identifiers or {}),
                keywords=list(keywords or []),
                services=list(service_names or []),
                severities=list(severities or []),
                time_range=time_range,
                limit=limit,
                cursor=cursor,
            )
            result = await services.runtime_evidence.search(scope, request)
            output = RuntimeEvidenceSearchOutput.from_domain(result)
            span.set_attribute("result_count", len(output.items))
            span.set_attribute("has_more", output.has_more)
            span.set_attribute("complete", output.completeness.complete)
            logger.info(
                "MCP search_runtime_evidence completed",
                extra={
                    "context": {
                        "tenant_id": scope.tenant_id,
                        "investigation_id": str(scope.investigation_id),
                        "correlation_id": scope.correlation_id,
                        "result_count": len(output.items),
                        "has_more": output.has_more,
                    }
                },
            )
            return output
        except Exception as exc:
            envelope = error_envelope(exc)
            span.set_attribute("error_code", envelope.error.code.value)
            logger.info(
                "MCP search_runtime_evidence failed",
                extra={
                    "context": {
                        "tenant_id": scope.tenant_id,
                        "investigation_id": str(scope.investigation_id),
                        "correlation_id": scope.correlation_id,
                        "error_code": envelope.error.code.value,
                        "retryable": envelope.error.retryable,
                    }
                },
            )
            return envelope


def register_search_runtime_evidence_tool(
    server: MCPServer, investigation_services: InvestigationServices
) -> None:
    """Register the tool; SDK derives the input schema from the signature."""

    @server.tool(
        name=TOOL_NAME,
        title=TOOL_TITLE,
        description=TOOL_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS,
        structured_output=True,
    )
    async def search_runtime_evidence(
        environment: EnvironmentParam,
        identifiers: IdentifiersParam,
        keywords: KeywordsParam,
        services: ServicesParam,
        severities: SeveritiesParam,
        start_time: datetime | None,
        end_time: datetime | None,
        limit: LimitParam,
        cursor: CursorParam,
    ) -> RuntimeEvidenceSearchOutput | McpErrorEnvelope:
        try:
            scope = require_mcp_context().to_scope()
        except Exception as exc:
            return error_envelope(exc)
        return await search_runtime_evidence_core(
            services=investigation_services,
            scope=scope,
            environment=environment,
            identifiers=identifiers,
            keywords=keywords,
            service_names=services,
            severities=severities,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            cursor=cursor,
        )
