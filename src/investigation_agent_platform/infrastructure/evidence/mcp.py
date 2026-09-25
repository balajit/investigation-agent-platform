# src/investigation_agent_platform/infrastructure/evidence/mcp.py
import hashlib
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from opentelemetry import trace

from investigation_agent_platform.domain.common.exceptions import (
    ExecutionError,
    SecurityPolicyViolationException,
)
from investigation_agent_platform.domain.evidence.models import (
    ClassificationLevel,
    Evidence,
    EvidenceType,
)
from investigation_agent_platform.domain.evidence.requests import RuntimeEvidenceRequest
from investigation_agent_platform.domain.profile.models import ObservabilityProfile
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)
from investigation_agent_platform.ports.evidence.gateway import EvidenceQueryResult

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class McpEvidenceAdapter:
    """Model Context Protocol (MCP) evidence adapter for client/server external tools."""

    def __init__(self, server_params: Any, allowed_tools: list[str]) -> None:
        self._server_params = server_params
        self._allowed_tools: set[str] = set(allowed_tools)

    async def search_runtime_evidence(
        self, tenant_id: str, investigation_id: UUID, request: RuntimeEvidenceRequest, profile: ObservabilityProfile
    ) -> EvidenceQueryResult:
        with tracer.start_as_current_span("McpEvidenceAdapter.search_runtime_evidence") as span:
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("investigation_id", str(investigation_id))

            try:
                from mcp import ClientSession
                from mcp.client.stdio import stdio_client
            except ImportError as exc:
                raise ExecutionError("mcp package is not installed.") from exc

            target_tool = "search_logs"
            if target_tool not in self._allowed_tools:
                raise SecurityPolicyViolationException(f"MCP Tool '{target_tool}' is not in allowed tools list.")

            try:
                async with stdio_client(self._server_params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(
                            target_tool,
                            arguments={
                                "tenant_id": tenant_id,
                                "investigation_id": str(investigation_id),
                                "environment": request.environment,
                                "query": request.query_string or "",
                                "limit": request.limit,
                            },
                        )
                        items = [self._map_mcp_result_to_evidence(item, tenant_id, investigation_id) for item in result.content]
                        return EvidenceQueryResult(
                            items=items,
                            cursor=None,
                            has_more=False,
                            total_count=len(items),
                        )
            except Exception as exc:
                logger.error("MCP tool invocation failed", exc_info=exc, extra={"context": {"tenant_id": tenant_id, "investigation_id": str(investigation_id)}})
                raise ExecutionError(f"MCP invocation failure: {exc}") from exc

    def _map_mcp_result_to_evidence(self, content_item: Any, tenant_id: str, investigation_id: UUID) -> Evidence:
        now = datetime.now(UTC)
        text_payload = getattr(content_item, "text", str(content_item))
        item_hash = hashlib.sha256(f"{tenant_id}:{investigation_id}:{text_payload}".encode()).hexdigest()

        prov = EvidenceProvenance(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            provider_type="MCP",
            requested_provider_id="mcp-server",
            actual_provider_id="mcp-server",
            source_system="MCP-Tool",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="MCP", operation="CALL_TOOL", normalized_query_hash=item_hash
            ),
            source_location=SourceLocation(system="MCP", identifier="mcp-tool"),
        )
        fresh = EvidenceFreshness(observed_at=now, retrieved_at=now)

        return Evidence(
            tenant_id=tenant_id,
            investigation_id=prov.investigation_id,
            evidence_type=EvidenceType.RUNTIME_LOG,
            provider="MCP",
            source="mcp://tool/search_logs",
            title="MCP Tool Result",
            summary=text_payload[:200],
            content_snippet=text_payload[:4000],
            content_uri=f"mcp://result/{item_hash}",
            fingerprint=item_hash,
            classification=ClassificationLevel.INTERNAL,
            observed_at=now,
            retrieved_at=now,
            attributes={"raw_payload": text_payload, "tenant_id": tenant_id},
            provenance=prov,
            freshness=fresh,
        )