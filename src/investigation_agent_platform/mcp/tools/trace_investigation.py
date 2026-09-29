# src/investigation_agent_platform/mcp/tools/trace_investigation.py
"""MCP ``investigate_trace`` tool (Part 9 Phase 6).

Investigates a trace within the current authenticated investigation and
returns bounded code locations, SQL statements, and table references. Every
derived finding retains source evidence IDs; results explicitly report
completeness and truncation.
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
from investigation_agent_platform.domain.evidence.requests import (
    ENVIRONMENT_PATTERN,
    MAX_TRACE_ID_LENGTH,
    TRACE_ID_PATTERN,
    TimeRange,
    TraceTelemetryRequest,
)
from investigation_agent_platform.mcp.context import require_mcp_context
from investigation_agent_platform.mcp.errors import McpErrorEnvelope, error_envelope
from investigation_agent_platform.mcp.schemas.trace import (
    InvestigateTraceOutput,
    TraceEvidenceRef,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

TOOL_NAME = "investigate_trace"
TOOL_TITLE = "Investigate Trace"
TOOL_DESCRIPTION = (
    "Investigate a trace within the current authenticated investigation and return "
    "bounded runtime evidence, code locations, SQL statements, and table references. "
    "Every derived finding retains source evidence IDs. "
    "Results explicitly report completeness and truncation."
)
TOOL_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)

TraceIdParam = Annotated[
    str, Field(min_length=1, max_length=MAX_TRACE_ID_LENGTH, pattern=TRACE_ID_PATTERN)
]
EnvironmentParam = Annotated[str, Field(min_length=1, max_length=100, pattern=ENVIRONMENT_PATTERN)]
MaxEvidenceParam = Annotated[int, Field(ge=1, le=100)]


async def investigate_trace_core(
    *,
    services: InvestigationServices,
    scope: InvestigationScope,
    trace_id: str,
    environment: str,
    start_time: datetime | None,
    end_time: datetime | None,
    max_evidence: int,
) -> InvestigateTraceOutput | McpErrorEnvelope:
    """Execute the trace investigation; failures become safe error envelopes."""
    with tracer.start_as_current_span("mcp.investigate_trace") as span:
        span.set_attribute("tool", TOOL_NAME)
        span.set_attribute("tenant_id", scope.tenant_id)
        span.set_attribute("investigation_id", str(scope.investigation_id))
        span.set_attribute("correlation_id", scope.correlation_id)
        span.set_attribute("trace_id", trace_id)
        span.set_attribute("environment", environment)
        try:
            time_range: TimeRange | None = None
            if start_time is not None or end_time is not None:
                if start_time is None or end_time is None:
                    raise DomainValidationException(
                        "Provide both start_time and end_time, or neither."
                    )
                time_range = TimeRange(start_time=start_time, end_time=end_time)
            request = TraceTelemetryRequest(
                trace_id=trace_id,
                environment=environment,
                time_range=time_range,
                max_evidence=max_evidence,
            )
            result = await services.trace_investigation.investigate_trace(scope, request)
            output = InvestigateTraceOutput.from_domain(
                result.telemetry,
                evidence_refs=[
                    TraceEvidenceRef(
                        evidence_id=ref.evidence_id,
                        evidence_type=ref.evidence_type,
                        summary=ref.summary,
                    )
                    for ref in result.evidence
                ],
            )
            span.set_attribute("evidence_count", output.completeness.examined_count)
            span.set_attribute("complete", output.completeness.complete)
            logger.info(
                "MCP investigate_trace completed",
                extra={
                    "context": {
                        "tenant_id": scope.tenant_id,
                        "investigation_id": str(scope.investigation_id),
                        "correlation_id": scope.correlation_id,
                        "trace_id": trace_id,
                        "complete": output.completeness.complete,
                    }
                },
            )
            return output
        except Exception as exc:
            envelope = error_envelope(exc)
            span.set_attribute("error_code", envelope.error.code.value)
            logger.info(
                "MCP investigate_trace failed",
                extra={
                    "context": {
                        "tenant_id": scope.tenant_id,
                        "investigation_id": str(scope.investigation_id),
                        "correlation_id": scope.correlation_id,
                        "trace_id": trace_id,
                        "error_code": envelope.error.code.value,
                    }
                },
            )
            return envelope


def register_investigate_trace_tool(server: MCPServer, services: InvestigationServices) -> None:
    """Register the tool; SDK derives the input schema from the signature."""

    @server.tool(
        name=TOOL_NAME,
        title=TOOL_TITLE,
        description=TOOL_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS,
        structured_output=True,
    )
    async def investigate_trace(
        trace_id: TraceIdParam,
        environment: EnvironmentParam,
        start_time: datetime | None,
        end_time: datetime | None,
        max_evidence: MaxEvidenceParam,
    ) -> InvestigateTraceOutput | McpErrorEnvelope:
        try:
            scope = require_mcp_context().to_scope()
        except Exception as exc:
            return error_envelope(exc)
        return await investigate_trace_core(
            services=services,
            scope=scope,
            trace_id=trace_id,
            environment=environment,
            start_time=start_time,
            end_time=end_time,
            max_evidence=max_evidence,
        )
