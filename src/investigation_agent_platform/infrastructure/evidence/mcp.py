# src/investigation_agent_platform/infrastructure/evidence/mcp.py
import asyncio
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

# F-042: broker-level bounds applied to every MCP tool invocation regardless
# of what the caller requested.
MCP_TOOL_TIMEOUT_SECONDS = 20.0
MCP_MAX_ITEMS_PER_CALL = 50
MCP_MAX_SNIPPET_BYTES = 4000
MCP_MAX_ARGUMENT_BYTES = 8192


class McpEvidenceAdapter:
    """Model Context Protocol (MCP) evidence adapter for client/server external tools."""

    def __init__(
        self,
        server_params: Any,
        allowed_tools: list[str],
        tool_timeout_seconds: float = MCP_TOOL_TIMEOUT_SECONDS,
        max_items_per_call: int = MCP_MAX_ITEMS_PER_CALL,
    ) -> None:
        self._server_params = server_params
        self._allowed_tools: set[str] = set(allowed_tools)
        self._tool_timeout_seconds = tool_timeout_seconds
        self._max_items_per_call = max_items_per_call

    def _validate_tool_call(
        self, tenant_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> None:
        """Strict pre-dispatch validation: allow-list, schema shape, size caps."""
        if tool_name not in self._allowed_tools:
            raise SecurityPolicyViolationException(
                f"MCP Tool '{tool_name}' is not in allowed tools list."
            )
        if not isinstance(arguments, dict):
            raise SecurityPolicyViolationException("MCP tool arguments must be a key-value mapping")
        try:
            import json as _json

            encoded = _json.dumps(arguments, default=str).encode("utf-8")
        except Exception as exc:
            raise SecurityPolicyViolationException(
                f"MCP tool arguments are not serializable: {exc}"
            ) from exc
        if len(encoded) > MCP_MAX_ARGUMENT_BYTES:
            raise SecurityPolicyViolationException(
                f"MCP tool arguments exceed {MCP_MAX_ARGUMENT_BYTES} bytes"
            )
        for key, value in arguments.items():
            if isinstance(value, str) and ("\x00" in value or "<script" in value.lower()):
                raise SecurityPolicyViolationException(
                    f"Forbidden pattern in MCP tool argument '{key}'"
                )

    async def search_runtime_evidence(
        self,
        tenant_id: str,
        investigation_id: UUID,
        request: RuntimeEvidenceRequest,
        profile: ObservabilityProfile,
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
            arguments = {
                "tenant_id": tenant_id,
                "investigation_id": str(investigation_id),
                "environment": request.environment,
                "query": request.query_string or "",
                "limit": min(request.limit, self._max_items_per_call),
            }
            self._validate_tool_call(tenant_id, target_tool, arguments)

            try:
                async with stdio_client(self._server_params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await asyncio.wait_for(
                            session.call_tool(target_tool, arguments=arguments),
                            timeout=self._tool_timeout_seconds,
                        )
                        content_items = list(result.content)[: self._max_items_per_call]
                        items = [
                            self._map_mcp_result_to_evidence(item, tenant_id, investigation_id)
                            for item in content_items
                        ]
                        logger.info(
                            "MCP tool invocation completed",
                            extra={
                                "context": {
                                    "tenant_id": tenant_id,
                                    "investigation_id": str(investigation_id),
                                    "tool": target_tool,
                                    "items": len(items),
                                }
                            },
                        )
                        return EvidenceQueryResult(
                            items=items,
                            cursor=None,
                            has_more=False,
                            total_count=len(items),
                        )
            except TimeoutError as exc:
                logger.error(
                    "MCP tool invocation timed out",
                    exc_info=exc,
                    extra={
                        "context": {
                            "tenant_id": tenant_id,
                            "investigation_id": str(investigation_id),
                        }
                    },
                )
                raise ExecutionError(
                    f"MCP invocation timed out after {self._tool_timeout_seconds}s"
                ) from exc
            except Exception as exc:
                logger.error(
                    "MCP tool invocation failed",
                    exc_info=exc,
                    extra={
                        "context": {
                            "tenant_id": tenant_id,
                            "investigation_id": str(investigation_id),
                        }
                    },
                )
                raise ExecutionError(f"MCP invocation failure: {exc}") from exc

    def _map_mcp_result_to_evidence(
        self, content_item: Any, tenant_id: str, investigation_id: UUID
    ) -> Evidence:
        now = datetime.now(UTC)
        text_payload = getattr(content_item, "text", str(content_item))
        item_hash = hashlib.sha256(
            f"{tenant_id}:{investigation_id}:{text_payload}".encode()
        ).hexdigest()

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
            content_snippet=text_payload[:MCP_MAX_SNIPPET_BYTES],
            content_uri=f"mcp://result/{item_hash}",
            fingerprint=item_hash,
            classification=ClassificationLevel.INTERNAL,
            observed_at=now,
            retrieved_at=now,
            attributes={"raw_payload": text_payload, "tenant_id": tenant_id},
            provenance=prov,
            freshness=fresh,
        )
