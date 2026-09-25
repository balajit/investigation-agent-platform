# src/investigation_agent_platform/ports/evidence/state.py
"""Transactional database state evidence provider port protocol."""

from typing import Protocol, runtime_checkable
from uuid import UUID

from investigation_agent_platform.domain.evidence.requests import ApplicationStateRequest
from investigation_agent_platform.domain.profile.models import StateProfile
from investigation_agent_platform.ports.evidence.gateway import EvidenceQueryResult


@runtime_checkable
class StateEvidenceProviderProtocol(Protocol):
    """Port for querying transactional database state."""

    async def get_application_state(
        self,
        tenant_id: str,
        investigation_id: UUID,
        request: ApplicationStateRequest,
        profile: StateProfile,
    ) -> EvidenceQueryResult: ...

    async def search_application_state(
        self,
        tenant_id: str,
        investigation_id: UUID,
        request: ApplicationStateRequest,
        profile: StateProfile,
    ) -> EvidenceQueryResult: ...
