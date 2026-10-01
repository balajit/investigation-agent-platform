# src/investigation_agent_platform/mcp/tools/reference_docs.py
"""MCP ``search_reference_docs`` tool (Part 11.6).

Investigation-scoped reference search: trusted context from the transport
(never tool arguments), one application-service call, domain-to-MCP
projection. No source ids, storage keys, or authorization inputs exist in
this contract. Pre-investigation search stays out of scope: the tool
requires the ambient investigation context.
"""

import logging
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from opentelemetry import trace
from pydantic import Field

from investigation_agent_platform.application.investigation.composition import (
    InvestigationServices,
)
from investigation_agent_platform.application.investigation.context import InvestigationScope
from investigation_agent_platform.mcp.context import require_mcp_context
from investigation_agent_platform.mcp.errors import McpErrorEnvelope, error_envelope
from investigation_agent_platform.mcp.schemas.reference import ReferenceDocsSearchOutput

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

TOOL_NAME = "search_reference_docs"
TOOL_TITLE = "Search Reference Documents"
TOOL_DESCRIPTION = (
    "Search indexed reference documentation within the current authenticated "
    "investigation. Results are automatically scoped to the authorized tenant. "
    "Use the returned next_cursor only with the same search criteria. "
    "This tool does not accept source ids, storage keys, or authorization context."
)
TOOL_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)

QParam = Annotated[str, Field(min_length=1, max_length=2000)]
KindsParam = Annotated[list[str] | None, Field(max_length=10)]
LimitParam = Annotated[int, Field(ge=1, le=100)]
CursorParam = Annotated[str | None, Field(min_length=1, max_length=4096)]


async def search_reference_docs_core(
    *,
    services: InvestigationServices,
    scope: InvestigationScope,
    q: str,
    kinds: list[str] | None,
    limit: int,
    cursor: str | None,
) -> ReferenceDocsSearchOutput | McpErrorEnvelope:
    """Execute the search; every failure becomes a safe error envelope."""
    with tracer.start_as_current_span("mcp.search_reference_docs") as span:
        span.set_attribute("tool", TOOL_NAME)
        span.set_attribute("tenant_id", scope.tenant_id)
        span.set_attribute("investigation_id", str(scope.investigation_id))
        try:
            service = getattr(services, "reference_docs", None)
            if service is None:
                raise RuntimeError("Reference-document search is unavailable")
            hits, next_cursor = await service.search(
                scope.tenant_id,
                q,
                kinds=list(kinds or []),
                cursor=cursor,
                limit=limit,
            )
            output = ReferenceDocsSearchOutput.from_hits(hits, next_cursor)
            span.set_attribute("result_count", len(output.items))
            logger.info(
                "MCP search_reference_docs completed",
                extra={
                    "context": {
                        "tenant_id": scope.tenant_id,
                        "investigation_id": str(scope.investigation_id),
                        "result_count": len(output.items),
                    }
                },
            )
            return output
        except Exception as exc:
            envelope = error_envelope(exc)
            span.set_attribute("error_code", envelope.error.code.value)
            logger.info(
                "MCP search_reference_docs failed",
                extra={
                    "context": {
                        "tenant_id": scope.tenant_id,
                        "investigation_id": str(scope.investigation_id),
                    }
                },
            )
            return envelope


def register_search_reference_docs_tool(
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
    async def search_reference_docs(
        q: QParam,
        kinds: KindsParam,
        limit: LimitParam,
        cursor: CursorParam,
    ) -> ReferenceDocsSearchOutput | McpErrorEnvelope:
        try:
            scope = require_mcp_context().to_scope()
        except Exception as exc:
            return error_envelope(exc)
        return await search_reference_docs_core(
            services=investigation_services,
            scope=scope,
            q=q,
            kinds=kinds,
            limit=limit,
            cursor=cursor,
        )
