# src/investigation_agent_platform/mcp/tools/evidence_detail.py
"""MCP ``get_evidence`` tool (Part 9 Phase 6).

Retrieves one evidence item from the current authenticated investigation.
The evidence ID is not an authorization credential: the application service
verifies investigation scope before returning anything.
"""

import logging
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from opentelemetry import trace

from investigation_agent_platform.application.investigation.composition import (
    InvestigationServices,
)
from investigation_agent_platform.application.investigation.context import InvestigationScope
from investigation_agent_platform.mcp.context import require_mcp_context
from investigation_agent_platform.mcp.errors import McpErrorEnvelope, error_envelope
from investigation_agent_platform.mcp.schemas.evidence import EvidenceDetailOutput

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

TOOL_NAME = "get_evidence"
TOOL_TITLE = "Get Evidence"
TOOL_DESCRIPTION = (
    "Retrieve one evidence item from the current authenticated investigation. "
    "The evidence ID is not an authorization credential; the server verifies "
    "that the item belongs to the authorized investigation scope."
)
TOOL_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)


async def get_evidence_core(
    *,
    services: InvestigationServices,
    scope: InvestigationScope,
    evidence_id: UUID,
) -> EvidenceDetailOutput | McpErrorEnvelope:
    """Execute the retrieval; every failure becomes a safe error envelope."""
    with tracer.start_as_current_span("mcp.get_evidence") as span:
        span.set_attribute("tool", TOOL_NAME)
        span.set_attribute("tenant_id", scope.tenant_id)
        span.set_attribute("investigation_id", str(scope.investigation_id))
        span.set_attribute("correlation_id", scope.correlation_id)
        try:
            evidence = await services.evidence_detail.get(scope, evidence_id)
            output = EvidenceDetailOutput.from_domain(evidence)
            logger.info(
                "MCP get_evidence completed",
                extra={
                    "context": {
                        "tenant_id": scope.tenant_id,
                        "investigation_id": str(scope.investigation_id),
                        "correlation_id": scope.correlation_id,
                    }
                },
            )
            return output
        except Exception as exc:
            envelope = error_envelope(exc)
            span.set_attribute("error_code", envelope.error.code.value)
            logger.info(
                "MCP get_evidence failed",
                extra={
                    "context": {
                        "tenant_id": scope.tenant_id,
                        "investigation_id": str(scope.investigation_id),
                        "correlation_id": scope.correlation_id,
                        "error_code": envelope.error.code.value,
                    }
                },
            )
            return envelope


def register_get_evidence_tool(server: MCPServer, services: InvestigationServices) -> None:
    """Register the tool; SDK derives the input schema from the signature."""

    @server.tool(
        name=TOOL_NAME,
        title=TOOL_TITLE,
        description=TOOL_DESCRIPTION,
        annotations=TOOL_ANNOTATIONS,
        structured_output=True,
    )
    async def get_evidence(
        evidence_id: UUID,
    ) -> EvidenceDetailOutput | McpErrorEnvelope:
        try:
            scope = require_mcp_context().to_scope()
        except Exception as exc:
            return error_envelope(exc)
        return await get_evidence_core(
            services=services,
            scope=scope,
            evidence_id=evidence_id,
        )
