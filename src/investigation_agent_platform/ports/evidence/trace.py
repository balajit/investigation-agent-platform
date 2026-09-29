# src/investigation_agent_platform/ports/evidence/trace.py
"""Trace telemetry extraction ports (Part 9).

Implemented by ``TraceTelemetryExtractor``. The application trace service
depends only on the derivation port: retrieval flows through the evidence
gateway (single sanitized path), derivation is pure.
"""

from typing import Protocol, runtime_checkable
from uuid import UUID

from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.domain.evidence.requests import TraceTelemetryRequest
from investigation_agent_platform.domain.evidence.telemetry import TraceTelemetry
from investigation_agent_platform.domain.profile.models import ObservabilityProfile


@runtime_checkable
class TraceTelemetryPort(Protocol):
    """Bounded, provenance-preserving trace investigation (retrieval)."""

    async def extract(
        self,
        *,
        tenant_id: str,
        investigation_id: UUID,
        request: TraceTelemetryRequest,
        profile: ObservabilityProfile,
        correlation_id: str | None = None,
    ) -> TraceTelemetry: ...


@runtime_checkable
class TraceTelemetryDerivationPort(Protocol):
    """Pure derivation of the bounded trace view from retrieved evidence."""

    def extract_from_items(
        self,
        *,
        trace_id: str,
        items: list[Evidence],
        skipped: int,
        has_more: bool,
        max_evidence: int,
        mapping_source_id: str | None = None,
    ) -> TraceTelemetry: ...
