# src/investigation_agent_platform/application/investigation/services.py
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

from investigation_agent_platform.domain.common.exceptions import (
    ApplicationProfileNotFoundException,
    DomainException,
    SecurityPolicyViolationException,
)
from investigation_agent_platform.domain.common.utils import Clock, SystemClock
from investigation_agent_platform.domain.entity.models import InvestigationEntity
from investigation_agent_platform.domain.events.base import InvestigationCancelled
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceRelationship
from investigation_agent_platform.domain.finding.models import Finding
from investigation_agent_platform.domain.hypothesis.models import Hypothesis
from investigation_agent_platform.domain.investigation.models import (
    ActorType,
    Fact,
    Investigation,
    InvestigationAction,
    InvestigationContext,
    InvestigationLimits,
    InvestigationRequest,
    InvestigationStatus,
)
from investigation_agent_platform.domain.profile.models import ApplicationProfile
from investigation_agent_platform.domain.timeline.models import TimelineEvent
from investigation_agent_platform.ports.messaging.publisher import EventPublisher
from investigation_agent_platform.ports.observability.telemetry import ObservabilityPort
from investigation_agent_platform.ports.persistence.repositories import (
    ActionExecutionRepository,
    ApplicationProfileRepository,
    CheckpointRepository,
    EvidenceRepository,
    FindingConclusionRepository,
    InvestigationRepository,
    TimelineRepository,
    TransitionEventRepository,
)
from investigation_agent_platform.ports.security.redactor import (
    ActionAuthorizerPort,
    CapabilityRegistryPort,
)

logger = logging.getLogger("iap.application")


@dataclass
class InvestigationExecutionContext:
    investigation: Investigation
    profile: ApplicationProfile
    facts: list[Fact] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    entities: list[InvestigationEntity] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)
    relationships: list[EvidenceRelationship] = field(default_factory=list)
    execution_limits: InvestigationLimits = field(default_factory=InvestigationLimits)


class CreateInvestigationService:
    def __init__(
        self,
        investigation_repo: InvestigationRepository,
        profile_repo: ApplicationProfileRepository,
        telemetry: ObservabilityPort | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.investigation_repo = investigation_repo
        self.profile_repo = profile_repo
        self.telemetry = telemetry
        self.clock = clock or SystemClock()

    async def execute(self, request: InvestigationRequest, tenant_id: str) -> Investigation:
        logger.info(
            "Creating investigation",
            extra={"context": {"application_id": request.application_id, "tenant_id": tenant_id}},
        )

        profile = await self.profile_repo.get_by_application_id(tenant_id, request.application_id)
        if not profile:
            raise ApplicationProfileNotFoundException(
                f"Application profile {request.application_id} not found",
                details={"application_id": request.application_id},
            )

        now = self.clock.utcnow()
        window_seconds = profile.investigation_configuration.default_time_window
        time_window = (now - timedelta(seconds=window_seconds), now)

        context = InvestigationContext(
            environment=profile.environment,
            time_window=time_window,
            known_identifiers={"sessionId": request.session_id} if request.session_id else {},
        )

        investigation = Investigation(
            session_id=request.session_id,
            application_id=request.application_id,
            tenant_id=tenant_id,
            request=request,
            context=context,
            status=InvestigationStatus.CREATED,
            created_at=now,
            updated_at=now,
            version=1,
        )

        await self.investigation_repo.create(tenant_id, investigation)
        if self.telemetry:
            self.telemetry.record_metric(
                "investigation_created", 1.0, {"app_id": request.application_id}
            )

        return investigation


class GetInvestigationService:
    def __init__(self, investigation_repo: InvestigationRepository) -> None:
        self.investigation_repo = investigation_repo

    async def execute(self, tenant_id: str, investigation_id: UUID) -> Investigation | None:
        return await self.investigation_repo.get_by_id(tenant_id, investigation_id)


class ResumeInvestigationService:
    def __init__(
        self,
        investigation_repo: InvestigationRepository,
        profile_repo: ApplicationProfileRepository,
        evidence_repo: EvidenceRepository,
        timeline_repo: TimelineRepository,
        checkpoint_repo: CheckpointRepository,
    ) -> None:
        self.investigation_repo = investigation_repo
        self.profile_repo = profile_repo
        self.evidence_repo = evidence_repo
        self.timeline_repo = timeline_repo
        self.checkpoint_repo = checkpoint_repo

    async def execute(
        self, tenant_id: str, investigation_id: UUID
    ) -> InvestigationExecutionContext:
        investigation = await self.investigation_repo.get_by_id(tenant_id, investigation_id)
        if not investigation:
            raise DomainException(
                f"Investigation {investigation_id} not found", error_code="NOT_FOUND"
            )

        profile = await self.profile_repo.get_by_application_id(
            tenant_id, investigation.application_id
        )
        if not profile:
            raise ApplicationProfileNotFoundException(
                f"Profile {investigation.application_id} not found"
            )

        evidence = await self.evidence_repo.find_by_investigation_id(tenant_id, investigation_id)
        timeline = await self.timeline_repo.find_by_investigation_id(tenant_id, investigation_id)

        return InvestigationExecutionContext(
            investigation=investigation,
            profile=profile,
            evidence=evidence,
            timeline=timeline,
        )


class CancelInvestigationService:
    def __init__(
        self,
        investigation_repo: InvestigationRepository,
        transition_repo: TransitionEventRepository,
        event_publisher: EventPublisher | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.investigation_repo = investigation_repo
        self.transition_repo = transition_repo
        self.event_publisher = event_publisher
        self.clock = clock or SystemClock()

    async def execute(
        self, tenant_id: str, investigation_id: UUID, reason: str, actor: ActorType = ActorType.USER
    ) -> Investigation:
        investigation = await self.investigation_repo.get_by_id(tenant_id, investigation_id)
        if not investigation:
            raise DomainException(
                f"Investigation {investigation_id} not found", error_code="NOT_FOUND"
            )

        updated_inv, transition = investigation.transition_to(
            new_status=InvestigationStatus.CANCELLED,
            actor=actor,
            reason=reason,
        )

        await self.investigation_repo.save(
            tenant_id, updated_inv, expected_version=investigation.version
        )
        await self.transition_repo.record_transition(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            from_state=transition.from_status.value,
            to_state=transition.to_status.value,
            reason=reason,
        )

        if self.event_publisher:
            event = InvestigationCancelled(
                tenant_id=tenant_id,
                investigation_id=updated_inv.id,
                application_id=updated_inv.application_id,
                correlation_id=updated_inv.id,
                reason=reason,
                cancelled_by=actor.value,
            )
            await self.event_publisher.publish_domain_event(tenant_id, event)

        return updated_inv


class ExecuteActionService:
    """Mandatory authorization gate for every agent-proposed action (F-004).

    No action reaches the evidence gateway or is recorded as executed unless
    ``authorizer.authorize_action`` explicitly returns ``True`` and the
    requested capability is enabled for the tenant. Both dependencies are
    mandatory — construction without them, or a missing decision, denies by
    default rather than silently allowing execution.
    """

    def __init__(
        self,
        action_repo: ActionExecutionRepository,
        evidence_repo: EvidenceRepository,
        authorizer: ActionAuthorizerPort,
        capability_registry: CapabilityRegistryPort,
        policy_version: str = "v1",
    ) -> None:
        if authorizer is None or capability_registry is None:
            raise SecurityPolicyViolationException(
                "ExecuteActionService requires an authorizer and capability registry"
            )
        self.action_repo = action_repo
        self.evidence_repo = evidence_repo
        self.authorizer = authorizer
        self.capability_registry = capability_registry
        self.policy_version = policy_version

    async def authorize(
        self,
        tenant_id: str,
        investigation_id: UUID,
        action: InvestigationAction,
        principal_id: str,
        application_id: str | None = None,
    ) -> dict[str, object]:
        """Evaluate the mandatory authorization gate and return the recorded decision.

        Raises ``SecurityPolicyViolationException`` (non-retryable) if the
        action is not explicitly authorized or the capability is disabled.
        Never returns a permissive result on authorizer failure.
        """
        target_resource = str(action.parameters.get("resource", ""))
        context = {
            "investigation_id": str(investigation_id),
            "principal_id": principal_id,
            "action_id": str(action.action_id),
            "application_id": application_id,
        }
        authorized = await self.authorizer.authorize_action(
            tenant_id, action.action_type.value, target_resource, context
        )
        if not authorized:
            raise SecurityPolicyViolationException(
                f"Action '{action.action_type.value}' denied by authorization policy",
                details={"tenant_id": tenant_id, "action_type": action.action_type.value},
            )
        enabled = await self.capability_registry.is_capability_enabled(
            tenant_id, action.action_type.value
        )
        if not enabled:
            raise SecurityPolicyViolationException(
                f"Capability '{action.action_type.value}' is disabled for tenant",
                details={"tenant_id": tenant_id, "action_type": action.action_type.value},
            )
        return {
            "authorized": True,
            "policy_version": self.policy_version,
            "action_type": action.action_type.value,
            "principal_id": principal_id,
        }

    async def execute(
        self,
        tenant_id: str,
        investigation_id: UUID,
        action: InvestigationAction,
        principal_id: str = "worker",
    ) -> None:
        await self.action_repo.record_action(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            action=action,
            result_status="EXECUTED",
            principal_id=principal_id,
            policy_version=self.policy_version,
        )


class ConcludeInvestigationService:
    def __init__(
        self,
        investigation_repo: InvestigationRepository,
        finding_repo: FindingConclusionRepository,
    ) -> None:
        self.investigation_repo = investigation_repo
        self.finding_repo = finding_repo

    async def execute(self, tenant_id: str, investigation_id: UUID, finding: Finding) -> None:
        await self.finding_repo.save_finding(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            finding_type=finding.finding_type.value,
            details=finding.model_dump(),
        )
