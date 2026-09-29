# src/investigation_agent_platform/infrastructure/evidence/logs/elastic_bridge.py
"""Compatibility facade over trace telemetry extraction (Part 9).

All derivation lives in ``trace_telemetry.TraceTelemetryExtractor`` so the
same semantics serve MCP tools, application services, and any other caller.
This bridge keeps the historical entry point stable while returning the new
bounded, provenance-preserving ``TraceTelemetry`` contract.
"""

import logging
from uuid import UUID

from investigation_agent_platform.domain.evidence.requests import (
    TimeRange,
    TraceTelemetryRequest,
)
from investigation_agent_platform.domain.evidence.telemetry import TraceTelemetry
from investigation_agent_platform.domain.profile.models import ObservabilityProfile
from investigation_agent_platform.infrastructure.configuration.mapping_registry import (
    MappingProfileRegistry,
)
from investigation_agent_platform.infrastructure.evidence.logs.trace_telemetry import (
    TraceTelemetryExtractor,
)
from investigation_agent_platform.infrastructure.evidence.runtime.elastic import (
    AsyncElasticAdapter,
)

logger = logging.getLogger(__name__)


class ElasticIncidentBridge:
    """Bridge service that extracts call stacks + SQL lineage for a trace."""

    def __init__(
        self,
        elastic_adapter: AsyncElasticAdapter,
        mapping_registry: MappingProfileRegistry | None = None,
    ) -> None:
        self._adapter = elastic_adapter
        self._extractor = TraceTelemetryExtractor(
            runtime_provider=elastic_adapter,
            mapping_registry=mapping_registry,
        )

    async def extract_runtime_telemetry_for_trace(
        self,
        tenant_id: str,
        investigation_id: UUID,
        trace_id: str,
        profile: ObservabilityProfile,
        environment: str = "production",
        time_range: TimeRange | None = None,
    ) -> TraceTelemetry:
        """Queries Elastic via AsyncElasticAdapter and extracts call stacks + SQL lineage.

        The profile's ``mapping_source_id`` selects the field contract, so
        retrieval and derivation resolve the same mapping.
        """
        request = TraceTelemetryRequest(
            trace_id=trace_id,
            environment=environment,
            time_range=time_range,
            mapping_source_id=profile.mapping_source_id,
        )
        return await self._extractor.extract(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            request=request,
            profile=profile,
        )
