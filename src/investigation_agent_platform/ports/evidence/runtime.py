# src/investigation_agent_platform/ports/evidence/runtime.py
"""Runtime telemetry evidence provider port protocol."""

from typing import Protocol, runtime_checkable
from uuid import UUID

from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.domain.evidence.requests import RuntimeEvidenceRequest
from investigation_agent_platform.domain.profile.models import ObservabilityProfile
from investigation_agent_platform.ports.evidence.gateway import EvidenceQueryResult
from investigation_agent_platform.ports.evidence.mapping import EvidenceMappingLookup


@runtime_checkable
class RuntimeEvidenceProviderProtocol(Protocol):
    """Port for querying runtime telemetry, logs, and traces."""

    async def search_runtime_evidence(
        self,
        tenant_id: str,
        investigation_id: UUID,
        request: RuntimeEvidenceRequest,
        profile: ObservabilityProfile,
        correlation_id: str | None = None,
    ) -> EvidenceQueryResult: ...

    async def get_runtime_evidence(
        self,
        tenant_id: str,
        investigation_id: UUID,
        evidence_id: UUID,
        environment: str,
        mapping_lookup: EvidenceMappingLookup,
        correlation_id: str | None = None,
        mapping_source_id: str | None = None,
    ) -> Evidence: ...
