# src/investigation_agent_platform/application/investigation/trace_investigation.py
"""Application service for trace-centered investigation (Part 9).

Retrieves trace evidence through the evidence gateway — the single
sanctioned evidence path, inheriting its authorization, value sanitization,
and dedup — then derives the bounded code/SQL/table view from the sanitized
items. Persisted hits keep the ``get_evidence`` round-trip working for
trace-derived records. Knows nothing about MCP transport.
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
from investigation_agent_platform.domain.evidence.requests import (
    RuntimeEvidenceRequest,
    TraceTelemetryRequest,
)
from investigation_agent_platform.domain.evidence.telemetry import (
    EvidenceRef,
    TraceTelemetry,
)
from investigation_agent_platform.ports.evidence.gateway import EvidenceGatewayProtocol
from investigation_agent_platform.ports.evidence.trace import TraceTelemetryDerivationPort
from investigation_agent_platform.ports.persistence.repositories import (
    ApplicationProfileRepository,
    EvidenceRepository,
    InvestigationRepository,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class TraceInvestigationResult(BaseModel):
    """Bounded trace view plus lightweight references to examined evidence."""

    model_config = ConfigDict(frozen=True)

    telemetry: TraceTelemetry
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=100)


class TraceInvestigationService:
    """Investigate one trace within the authorized investigation scope."""

    def __init__(
        self,
        *,
        gateway: EvidenceGatewayProtocol,
        investigation_repo: InvestigationRepository,
        evidence_repo: EvidenceRepository,
        profile_repo: ApplicationProfileRepository,
        derivation: TraceTelemetryDerivationPort,
    ) -> None:
        self._gateway = gateway
        self._investigation_repo = investigation_repo
        self._evidence_repo = evidence_repo
        self._profile_repo = profile_repo
        self._derivation = derivation

    async def investigate_trace(
        self, context: InvestigationScope, request: TraceTelemetryRequest
    ) -> TraceInvestigationResult:
        """Return the bounded code/SQL/table view for a trace."""
        with tracer.start_as_current_span("TraceInvestigationService.investigate_trace") as span:
            span.set_attribute("tenant_id", context.tenant_id)
            span.set_attribute("investigation_id", str(context.investigation_id))
            span.set_attribute("trace_id", request.trace_id)
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
            # Part 10: environment selects the profile/mapping — a mismatch
            # is agent error, rejected before any provider I/O.
            if request.environment != profile.environment:
                raise DomainValidationException(
                    f"Request environment {request.environment!r} does not match "
                    f"the investigation profile {profile.environment!r}."
                )
            mapping_source_id = profile.observability_configuration.mapping_source_id
            # Investigation policy caps the agent-supplied budget; the
            # extractor reports which bound truncated via completeness.
            effective_max = min(
                request.max_evidence,
                profile.investigation_configuration.max_evidence_per_query,
            )
            evidence_request = RuntimeEvidenceRequest(
                environment=request.environment,
                identifiers={"trace_id": request.trace_id},
                time_range=request.time_range,
                limit=effective_max,
                mapping_source_id=mapping_source_id,
            )
            result = await self._gateway.search_runtime_evidence(
                context.tenant_id,
                context.investigation_id,
                investigation.application_id,
                evidence_request,
            )
            if result.items:
                await self._evidence_repo.save_batch(
                    context.tenant_id, list(result.items), context.investigation_id
                )
            metadata = result.execution_metadata or {}
            errors = metadata.get("errors")
            skipped = len(errors) if isinstance(errors, list) else 0
            telemetry = self._derivation.extract_from_items(
                trace_id=request.trace_id,
                items=list(result.items),
                skipped=skipped,
                has_more=result.has_more,
                max_evidence=effective_max,
                mapping_source_id=mapping_source_id,
            )
            contributing = {
                evidence_id
                for location in telemetry.code_locations
                for evidence_id in location.evidence_ids
            } | {
                evidence_id
                for table in telemetry.tables
                for evidence_id in table.evidence_ids
            } | {statement.evidence_id for statement in telemetry.sql_statements}
            by_id = {item.evidence_id: item for item in result.items}
            evidence_refs = [
                EvidenceRef(
                    evidence_id=evidence_id,
                    evidence_type=by_id[evidence_id].evidence_type.value,
                    summary=by_id[evidence_id].summary,
                )
                for evidence_id in sorted(contributing)
                if evidence_id in by_id
            ][:100]
            span.set_attribute("evidence_count", telemetry.evidence_count)
            span.set_attribute("complete", telemetry.completeness.complete)
            return TraceInvestigationResult(telemetry=telemetry, evidence=evidence_refs)
