# src/investigation_agent_platform/ports/protocols.py
"""Aggregated central port protocols re-exporting canonical interfaces."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.domain.profile.models import ApplicationProfile
from investigation_agent_platform.ports.correlation.engine import (
    CorrelationEngine,
    CorrelationExpander,
)
from investigation_agent_platform.ports.evidence.code import (
    CodeEvidenceProviderProtocol,
    CodeIntelligenceProviderProtocol,
)
from investigation_agent_platform.ports.evidence.gateway import (
    EvidenceGatewayProtocol,
    EvidenceQueryResult,
)
from investigation_agent_platform.ports.evidence.runtime import RuntimeEvidenceProviderProtocol
from investigation_agent_platform.ports.evidence.state import StateEvidenceProviderProtocol
from investigation_agent_platform.ports.evidence.store import EvidenceStorePort
from investigation_agent_platform.ports.messaging.publisher import EventPublisher
from investigation_agent_platform.ports.observability.telemetry import ObservabilityPort
from investigation_agent_platform.ports.profile.registry import ProfileRegistryPort
from investigation_agent_platform.ports.reasoning.reasoner import InvestigationReasoner
from investigation_agent_platform.ports.security.redactor import (
    ActionAuthorizerPort,
    EvidenceSanitizerPort,
    QueryPolicyPort,
)

__all__ = [
    "ActionAuthorizerPort",
    "CodeEvidenceProviderProtocol",
    "CodeIntelligenceProviderProtocol",
    "CorrelationEngine",
    "CorrelationExpander",
    "EventPublisher",
    "EvidenceGatewayProtocol",
    "EvidenceQueryResult",
    "EvidenceSanitizerPort",
    "EvidenceStorePort",
    "InvestigationContext",
    "InvestigationReasoner",
    "InvestigationTriggerPort",
    "ObservabilityPort",
    "ProfileRegistryPort",
    "QueryPolicyPort",
    "RuntimeEvidenceProviderProtocol",
    "StateEvidenceProviderProtocol",
]


@runtime_checkable
class InvestigationTriggerPort(Protocol):
    """Inbound port for starting investigation execution."""

    async def trigger(self, tenant_id: str, request: dict[str, Any]) -> UUID:
        ...


@runtime_checkable
class InvestigationContext(Protocol):
    """Aggregate investigation-context port reading tenant-correlated evidence."""

    async def get_application_profile(self, tenant_id: str, application_id: str) -> ApplicationProfile:
        ...

    async def get_runtime_evidence(self, tenant_id: str, investigation_id: UUID) -> list[Evidence]:
        ...

    async def search_evidence(self, tenant_id: str, query: str) -> EvidenceQueryResult:
        ...