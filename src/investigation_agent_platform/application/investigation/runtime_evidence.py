# src/investigation_agent_platform/application/investigation/runtime_evidence.py
"""Application service for bounded runtime evidence search (Part 9).

Thin policy layer over the evidence gateway: merges trusted context with the
agent-controlled request, resolves the investigation's application and
profile, delegates provider execution (limits stay authoritative downstream),
persists returned items so ``get_evidence`` round-trips work, and normalizes
completeness. Knows nothing about MCP transport. Provider-limit and
correlation plumbing gaps in the gateway contract are noted, not worked
around here.
"""

import logging

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.application.investigation.context import (
    InvestigationScope,
    require_investigation,
)
from investigation_agent_platform.domain.common.exceptions import (
    ApplicationProfileNotFoundException,
    DomainValidationException,
)
from investigation_agent_platform.domain.evidence.completeness import (
    CompletenessReason,
    EvidenceCompleteness,
)
from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.domain.evidence.requests import RuntimeEvidenceRequest
from investigation_agent_platform.ports.evidence.gateway import (
    EvidenceGatewayProtocol,
    EvidenceQueryResult,
)
from investigation_agent_platform.ports.persistence.repositories import (
    ApplicationProfileRepository,
    EvidenceRepository,
    InvestigationRepository,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class RuntimeEvidenceSearchResult(BaseModel):
    """Application-level search result with explicit completeness."""

    model_config = ConfigDict(frozen=True)

    items: list[Evidence] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False
    total_count: int = 0
    completeness: EvidenceCompleteness

    @classmethod
    def from_query_result(cls, result: EvidenceQueryResult) -> "RuntimeEvidenceSearchResult":
        """Derive completeness from the provider envelope.

        A page with ``has_more`` is explicitly *not* the complete answer
        (``pagination_available``); malformed skips mark the page incomplete
        without claiming truncation by bounds.
        """
        metadata = result.execution_metadata or {}
        errors = metadata.get("errors")
        error_count = len(errors) if isinstance(errors, list) else 0
        partial = bool(metadata.get("partial_result")) or error_count > 0
        if result.has_more:
            completeness = EvidenceCompleteness(
                complete=False,
                truncated=False,
                reason=CompletenessReason.PAGINATION_AVAILABLE,
                returned_count=len(result.items),
                examined_count=len(result.items) + error_count,
            )
        elif partial:
            completeness = EvidenceCompleteness(
                complete=False,
                truncated=False,
                reason=CompletenessReason.METADATA_INCOMPLETE,
                returned_count=len(result.items),
                examined_count=len(result.items) + error_count,
            )
        else:
            completeness = EvidenceCompleteness(
                complete=True,
                returned_count=len(result.items),
                examined_count=len(result.items),
            )
        return cls(
            items=list(result.items),
            next_cursor=result.cursor,
            has_more=result.has_more,
            total_count=result.total_count,
            completeness=completeness,
        )


class RuntimeEvidenceService:
    """Search runtime evidence within one trusted investigation scope."""

    def __init__(
        self,
        *,
        gateway: EvidenceGatewayProtocol,
        investigation_repo: InvestigationRepository,
        evidence_repo: EvidenceRepository,
        profile_repo: ApplicationProfileRepository,
    ) -> None:
        self._gateway = gateway
        self._investigation_repo = investigation_repo
        self._evidence_repo = evidence_repo
        self._profile_repo = profile_repo

    async def search(
        self, context: InvestigationScope, request: RuntimeEvidenceRequest
    ) -> RuntimeEvidenceSearchResult:
        """Search bounded evidence; persist hits for later retrieval."""
        with tracer.start_as_current_span("RuntimeEvidenceService.search") as span:
            span.set_attribute("tenant_id", context.tenant_id)
            span.set_attribute("investigation_id", str(context.investigation_id))
            investigation = await require_investigation(
                self._investigation_repo, context.tenant_id, context.investigation_id
            )
            profile = await self._profile_repo.get_by_application_id(
                context.tenant_id, investigation.application_id
            )
            if profile is None:
                raise ApplicationProfileNotFoundException(
                    f"No application profile for '{investigation.application_id}'."
                )
            # Part 10: environment no longer interpolates into index names —
            # it selects the profile/mapping. A mismatch between the agent's
            # claim and the investigation's profile is agent error, rejected
            # before any provider I/O (never silently re-scoped).
            if request.environment != profile.environment:
                raise DomainValidationException(
                    f"Request environment {request.environment!r} does not match "
                    f"the investigation profile {profile.environment!r}."
                )
            scoped_request = request.model_copy(
                update={
                    "mapping_source_id": profile.observability_configuration.mapping_source_id
                }
            )
            result = await self._gateway.search_runtime_evidence(
                context.tenant_id,
                context.investigation_id,
                investigation.application_id,
                scoped_request,
            )
            if result.items:
                # Identity mapping for the Q2 round-trip: persisted rows let
                # `get_evidence` resolve platform IDs without provider guessing.
                await self._evidence_repo.save_batch(
                    context.tenant_id, list(result.items), context.investigation_id
                )
            search_result = RuntimeEvidenceSearchResult.from_query_result(result)
            span.set_attribute("result_count", len(search_result.items))
            span.set_attribute("has_more", search_result.has_more)
            return search_result
