# src/investigation_agent_platform/application/investigation/composition.py
"""Composition root for investigation application services (Part 9).

Bundles the three transport-independent services behind one object so MCP
servers, REST routers, CLIs, and workers share identical behavior. Wired by
``bootstrap._wire_evidence_gateway`` for production; tests compose fakes
directly.
"""

from dataclasses import dataclass

from investigation_agent_platform.application.evidence.selector import (
    EvidenceProviderSelector,
)
from investigation_agent_platform.application.investigation.evidence_detail import (
    EvidenceDetailService,
)
from investigation_agent_platform.application.investigation.runtime_evidence import (
    RuntimeEvidenceService,
)
from investigation_agent_platform.application.investigation.trace_investigation import (
    TraceInvestigationService,
)
from investigation_agent_platform.ports.evidence.gateway import EvidenceGatewayProtocol
from investigation_agent_platform.ports.evidence.trace import TraceTelemetryDerivationPort
from investigation_agent_platform.ports.persistence.repositories import (
    ApplicationProfileRepository,
    EvidenceRepository,
    InvestigationRepository,
)
from investigation_agent_platform.ports.security.redactor import EvidenceSanitizerPort


@dataclass
class InvestigationServices:
    """Transport-independent investigation capabilities."""

    runtime_evidence: RuntimeEvidenceService
    evidence_detail: EvidenceDetailService
    trace_investigation: TraceInvestigationService


def build_investigation_services(
    *,
    gateway: EvidenceGatewayProtocol,
    selector: EvidenceProviderSelector,
    investigation_repo: InvestigationRepository,
    evidence_repo: EvidenceRepository,
    profile_repo: ApplicationProfileRepository,
    sanitizer: EvidenceSanitizerPort,
    derivation: TraceTelemetryDerivationPort,
) -> InvestigationServices:
    """Compose the investigation services from ports and repositories."""
    return InvestigationServices(
        runtime_evidence=RuntimeEvidenceService(
            gateway=gateway,
            investigation_repo=investigation_repo,
            evidence_repo=evidence_repo,
            profile_repo=profile_repo,
        ),
        evidence_detail=EvidenceDetailService(
            selector=selector,
            investigation_repo=investigation_repo,
            evidence_repo=evidence_repo,
            profile_repo=profile_repo,
            sanitizer=sanitizer,
        ),
        trace_investigation=TraceInvestigationService(
            gateway=gateway,
            investigation_repo=investigation_repo,
            evidence_repo=evidence_repo,
            profile_repo=profile_repo,
            derivation=derivation,
        ),
    )
