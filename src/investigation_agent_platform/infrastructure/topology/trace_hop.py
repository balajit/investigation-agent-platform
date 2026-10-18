# src/investigation_agent_platform/infrastructure/topology/trace_hop.py
"""``TraceHopResolverPort`` backed by the tenant-scoped evidence gateway
(ISSUE-5).

Resolves the cross-service target of one distributed-trace span using only
runtime evidence already retrievable through ``AsyncEvidenceGateway`` — the
mandatory enforcement boundary for every evidence read in this platform.
Never falls back to name-similarity or static-endpoint guessing: a trace
with no other-service span, or with more than one ambiguous target service,
resolves to ``None`` rather than fabricating a hop.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from investigation_agent_platform.domain.evidence.requests import RuntimeEvidenceRequest
from investigation_agent_platform.domain.observability.mapping import (
    FieldMapping,
    ObservabilitySourceMapping,
)
from investigation_agent_platform.domain.topology.models import TraceHopTarget
from investigation_agent_platform.infrastructure.evidence.runtime.field_resolver import (
    resolve_value,
)

logger = logging.getLogger(__name__)


def _nested_get(attributes: dict[str, Any], *path: str) -> str | None:
    """Reads either nested ECS shape (``{"span": {"id": "x"}}``) or a flat
    dotted key (``{"span.id": "x"}``) — the Elastic adapter's raw ``_source``
    passthrough may present either, depending on the index mapping."""
    dotted = ".".join(path)
    if dotted in attributes and isinstance(attributes[dotted], str):
        return str(attributes[dotted])
    node: Any = attributes
    for segment in path:
        if not isinstance(node, dict) or segment not in node:
            return None
        node = node[segment]
    return node if isinstance(node, str) else None


def _resolve_hop_field(
    attributes: dict[str, Any],
    mapping: ObservabilitySourceMapping | None,
    field_mapping: FieldMapping | None,
    legacy_path: tuple[str, ...],
) -> str | None:
    """Resolve one hop field via the mapping, or the legacy ECS path."""
    if mapping is not None and mapping.trace_hop is not None and field_mapping is not None:
        value = resolve_value(attributes, field_mapping)
        return value if isinstance(value, str) and value else None
    return _nested_get(attributes, *legacy_path)


class GatewayTraceHopResolver:
    """Queries trace evidence for a trace_id via the evidence gateway and
    extracts a corroborated other-service span, if exactly one exists.

    Part 10: an optional mapping makes hop-field resolution source-aware
    (service/span/parent paths resolve through it, and the trace request is
    stamped so retrieval uses the same mapping). Without one, the legacy ECS
    paths apply byte-identically. Sources without tracing-shaped data
    resolve to ``None`` — never guessed.
    """

    def __init__(
        self,
        gateway: Any,
        mapping: ObservabilitySourceMapping | None = None,
    ) -> None:
        self._gateway = gateway
        self._mapping = mapping

    async def resolve_target_service(
        self,
        tenant_id: str,
        investigation_id: UUID,
        application_id: str,
        environment: str,
        trace_id: str,
        source_span_id: str | None,
    ) -> TraceHopTarget | None:
        if not trace_id:
            return None
        try:
            request = RuntimeEvidenceRequest(
                environment=environment,
                identifiers={"trace_id": trace_id},
                limit=100,
            )
            if self._mapping is not None:
                request = request.model_copy(update={"mapping_source_id": self._mapping.source_id})
            result = await self._gateway.search_runtime_evidence(
                tenant_id,
                investigation_id,
                application_id,
                request,
            )
        except Exception as exc:
            logger.warning(
                "Trace hop lookup failed; no hop will be attempted",
                extra={"tenant_id": tenant_id, "trace_id": trace_id, "error": str(exc)},
            )
            return None

        candidates: dict[tuple[str, str], str | None] = {}
        hop = self._mapping.trace_hop if self._mapping is not None else None
        for item in getattr(result, "items", []):
            attributes = getattr(item, "attributes", None)
            if not isinstance(attributes, dict):
                continue
            span_id = _resolve_hop_field(
                attributes, self._mapping, hop.span_id if hop else None, ("span", "id")
            )
            service_name = _resolve_hop_field(
                attributes, self._mapping, hop.service if hop else None, ("service", "name")
            )
            if not span_id or not service_name:
                continue
            if source_span_id is not None and span_id == source_span_id:
                continue
            parent_id = _resolve_hop_field(
                attributes,
                self._mapping,
                hop.parent_id if hop and hop.parent_id else None,
                ("parent", "id"),
            )
            candidates[(service_name, span_id)] = parent_id

        distinct_services = {service for service, _ in candidates}
        if len(candidates) != 1:
            # Zero other-service spans (no hop), or more than one candidate
            # target span/service — never guess which one is real.
            if len(distinct_services) > 1 or len(candidates) > 1:
                logger.debug(
                    "Trace hop ambiguous: %d candidate target spans for trace_id=%s",
                    len(candidates),
                    trace_id,
                )
            return None

        (service_name, span_id), parent_id = next(iter(candidates.items()))
        return TraceHopTarget(
            target_service=service_name, target_span_id=span_id, parent_span_id=parent_id
        )
