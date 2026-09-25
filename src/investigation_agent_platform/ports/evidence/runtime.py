# src/investigation_agent_platform/ports/evidence/runtime.py
"""Runtime telemetry evidence provider port protocol."""

from typing import Protocol, runtime_checkable
from uuid import UUID

from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.domain.evidence.requests import RuntimeEvidenceRequest
from investigation_agent_platform.domain.profile.models import ObservabilityProfile
from investigation_agent_platform.ports.evidence.gateway import EvidenceQueryResult


@runtime_checkable
class RuntimeEvidenceProviderProtocol(Protocol):
    """Port for querying runtime telemetry, logs, and traces."""

    async def search_runtime_evidence(
        self,
        tenant_id: str,
        investigation_id: UUID,
        request: RuntimeEvidenceRequest,
        profile: ObservabilityProfile,
    ) -> EvidenceQueryResult: ...

    async def get_runtime_evidence(self, tenant_id: str, evidence_id: UUID) -> Evidence: ...
