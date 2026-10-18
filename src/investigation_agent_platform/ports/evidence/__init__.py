# src/investigation_agent_platform/ports/evidence/__init__.py
"""Evidence provider port protocols and query envelope contracts."""

from investigation_agent_platform.ports.evidence.code import (
    CodeEvidenceProviderProtocol,
    CodeIntelligenceProviderProtocol,
)
from investigation_agent_platform.ports.evidence.gateway import (
    EvidenceGatewayProtocol,
    EvidenceQueryResult,
)
from investigation_agent_platform.ports.evidence.mapping import EvidenceMappingLookup
from investigation_agent_platform.ports.evidence.runtime import RuntimeEvidenceProviderProtocol
from investigation_agent_platform.ports.evidence.state import StateEvidenceProviderProtocol
from investigation_agent_platform.ports.evidence.store import EvidenceStorePort
from investigation_agent_platform.ports.evidence.trace import (
    TraceTelemetryDerivationPort,
    TraceTelemetryPort,
)

__all__ = [
    "CodeEvidenceProviderProtocol",
    "CodeIntelligenceProviderProtocol",
    "EvidenceGatewayProtocol",
    "EvidenceMappingLookup",
    "EvidenceQueryResult",
    "EvidenceStorePort",
    "RuntimeEvidenceProviderProtocol",
    "StateEvidenceProviderProtocol",
    "TraceTelemetryDerivationPort",
    "TraceTelemetryPort",
]
