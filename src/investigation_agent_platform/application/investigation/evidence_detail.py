# src/investigation_agent_platform/application/investigation/evidence_detail.py
"""Application service for single-evidence retrieval (Part 9).

Enforces the investigation-level retrieval semantics the provider port
cannot: the platform evidence ID is resolved through the persisted identity
mapping, ownership against the trusted context is verified with no
disclosure on mismatch, and the record is re-fetched fresh from the provider
and re-sanitized before return. The evidence ID is never authorization.
"""

import logging
from uuid import UUID

from opentelemetry import trace

from investigation_agent_platform.application.evidence.selector import (
    EvidenceProviderSelector,
)
from investigation_agent_platform.application.investigation.context import (
    InvestigationScope,
    require_investigation,
)
from investigation_agent_platform.domain.common.exceptions import (
    ApplicationProfileNotFoundException,
    EvidenceNotFoundException,
)
from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.ports.persistence.repositories import (
    ApplicationProfileRepository,
    EvidenceRepository,
    InvestigationRepository,
)
from investigation_agent_platform.ports.security.redactor import EvidenceSanitizerPort

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class _RepositoryMappingLookup:
    """Adapt the evidence repository to the provider's mapping-lookup port."""

    def __init__(self, tenant_id: str, evidence_repo: EvidenceRepository) -> None:
        self._tenant_id = tenant_id
        self._evidence_repo = evidence_repo

    async def get_evidence(self, tenant_id: str, evidence_id: UUID) -> Evidence | None:
        if tenant_id != self._tenant_id:
            return None
        return await self._evidence_repo.get_by_id(tenant_id, evidence_id)


class EvidenceDetailService:
    """Retrieve one evidence item inside the authorized investigation scope."""

    def __init__(
        self,
        *,
        selector: EvidenceProviderSelector,
        investigation_repo: InvestigationRepository,
        evidence_repo: EvidenceRepository,
        profile_repo: ApplicationProfileRepository,
        sanitizer: EvidenceSanitizerPort,
    ) -> None:
        self._selector = selector
        self._investigation_repo = investigation_repo
        self._evidence_repo = evidence_repo
        self._profile_repo = profile_repo
        self._sanitizer = sanitizer

    async def get(self, context: InvestigationScope, evidence_id: UUID) -> Evidence:
        """Return the evidence record after verifying scope and re-fetching."""
        with tracer.start_as_current_span("EvidenceDetailService.get") as span:
            span.set_attribute("tenant_id", context.tenant_id)
            span.set_attribute("investigation_id", str(context.investigation_id))
            investigation = await require_investigation(
                self._investigation_repo, context.tenant_id, context.investigation_id
            )
            # Ownership first, from the mapping — no provider I/O before the
            # scope check, and no disclosure on mismatch.
            mapping = await self._evidence_repo.get_by_id(context.tenant_id, evidence_id)
            if (
                mapping is None
                or mapping.tenant_id != context.tenant_id
                or mapping.investigation_id != context.investigation_id
            ):
                raise EvidenceNotFoundException(f"Evidence {evidence_id} not found")
            profile = await self._profile_repo.get_by_application_id(
                context.tenant_id, investigation.application_id
            )
            if profile is None:
                raise ApplicationProfileNotFoundException(
                    f"No application profile for '{investigation.application_id}'."
                )
            provider = await self._selector.get_runtime_provider(
                context.tenant_id, investigation.application_id, profile.environment
            )
            evidence = await provider.get_runtime_evidence(
                context.tenant_id,
                context.investigation_id,
                evidence_id,
                profile.environment,
                _RepositoryMappingLookup(context.tenant_id, self._evidence_repo),
                correlation_id=context.correlation_id,
                mapping_source_id=profile.observability_configuration.mapping_source_id,
            )
            return await self._sanitizer.sanitize_evidence(context.tenant_id, evidence)
