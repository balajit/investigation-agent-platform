# src/investigation_agent_platform/domain/investigation/models.py
"""Investigation aggregate root, actions, limits, and runtime state models."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.common.exceptions import (
    InvalidLifecycleTransitionException,
)
from investigation_agent_platform.domain.common.utils import (
    Clock,
    SystemClock,
    generate_execution_hash,
)
from investigation_agent_platform.domain.entity.models import InvestigationEntity
from investigation_agent_platform.domain.evidence.models import (
    Evidence,
    EvidenceReference,
    EvidenceRelationship,
    EvidenceType,
)
from investigation_agent_platform.domain.finding.models import Finding, InvestigationConclusion
from investigation_agent_platform.domain.hypothesis.models import Hypothesis
from investigation_agent_platform.domain.timeline.models import TimelineEvent


class InvestigationStatus(StrEnum):
    CREATED = "CREATED"
    CONTEXTUALIZING = "CONTEXTUALIZING"
    INVESTIGATING = "INVESTIGATING"
    CORRELATING = "CORRELATING"
    HYPOTHESIZING = "HYPOTHESIZING"
    VERIFYING = "VERIFYING"
    CONCLUDING = "CONCLUDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"
    # Part 11.5: workflow is suspended waiting for caller-supplied input
    # (see domain/investigation/input_requirements.py). Terminal-until-resumed.
    AWAITING_INPUT = "AWAITING_INPUT"


class FactType(StrEnum):
    OBSERVATION = "OBSERVATION"
    STATE = "STATE"
    EVENT = "EVENT"
    ERROR = "ERROR"
    CODE_BEHAVIOR = "CODE_BEHAVIOR"
    CONFIGURATION = "CONFIGURATION"
    RELATIONSHIP = "RELATIONSHIP"


class ActionType(StrEnum):
    SEARCH_LOGS = "SEARCH_LOGS"
    QUERY_STATE = "QUERY_STATE"
    GET_CODE = "GET_CODE"
    CORRELATE = "CORRELATE"
    FORMULATE_HYPOTHESIS = "FORMULATE_HYPOTHESIS"
    VERIFY_HYPOTHESIS = "VERIFY_HYPOTHESIS"
    CONCLUDE = "CONCLUDE"


class ActorType(StrEnum):
    SYSTEM = "SYSTEM"
    ORCHESTRATOR = "ORCHESTRATOR"
    AGENT = "AGENT"
    USER = "USER"
    ADMIN = "ADMIN"


class CorrelationVector(BaseModel):
    model_config = ConfigDict(frozen=True)

    vector_id: UUID = Field(default_factory=uuid4)
    source_entity_id: UUID
    target_entity_id: UUID
    relationship_type: str = Field(..., max_length=128)
    weight: float = Field(default=1.0, ge=0.0, le=1.0)


class InvestigationObjective(BaseModel):
    model_config = ConfigDict(frozen=True)

    objective_id: UUID = Field(default_factory=uuid4)
    title: str = Field(..., max_length=256)
    description: str = Field(..., max_length=2048)
    target_system: str = Field(..., max_length=256)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EvidenceManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_id: UUID = Field(default_factory=uuid4)
    evidence_type: EvidenceType
    source: str = Field(..., description="URN or URI of source provider", max_length=512)
    payload_reference: str = Field(..., description="Tier 2 blob storage path", max_length=1024)
    summary: str = Field(..., description="Sanitized structural summary", max_length=2048)
    sanitized: bool = Field(default=True)
    relevance: float = Field(default=1.0, ge=0.0, le=1.0)
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class InvestigationAction(BaseModel):
    """Typed action intent proposed by LLM reasoner."""

    model_config = ConfigDict(frozen=True)

    action_id: UUID = Field(default_factory=uuid4)
    action_type: ActionType
    parameters: dict[str, Any] = Field(default_factory=dict)
    execution_hash: str = Field(..., max_length=128)

    @classmethod
    def create(
        cls,
        tenant_id: str,
        investigation_id: UUID,
        action_type: ActionType,
        parameters: dict[str, Any],
        capability: str = "default",
        provider: str = "default",
    ) -> "InvestigationAction":
        """Factory deriving deterministic fingerprint server-side.

        F-057: ``action_id`` is derived deterministically (UUIDv5 over the
        execution hash) so Temporal activity retries of the same logical
        action resolve to the same idempotency key instead of recording
        duplicate executions.
        """
        from uuid import NAMESPACE_URL, uuid5

        exec_hash = generate_execution_hash(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            action_type=action_type.value,
            parameters=parameters,
            capability=capability,
            provider=provider,
        )
        return cls(
            action_id=uuid5(NAMESPACE_URL, f"{tenant_id}:{investigation_id}:{exec_hash}"),
            action_type=action_type,
            parameters=parameters,
            execution_hash=exec_hash,
        )


class Fact(BaseModel):
    """Verified domain observation derived from evidence."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    statement: str = Field(..., max_length=2048)
    fact_type: FactType
    source_evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    attributes: dict[str, Any] = Field(default_factory=dict)


class InvestigationRequest(BaseModel):
    """User request triggering an investigation execution."""

    model_config = ConfigDict(frozen=True)

    problem_description: str = Field(..., max_length=4096)
    application_id: str = Field(..., max_length=128)
    session_id: str = Field(..., max_length=128)
    requested_by: str = Field(..., max_length=128)
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    priority: str = Field(default="NORMAL", max_length=32)
    parameters: dict[str, Any] = Field(default_factory=dict)


class InvestigationContext(BaseModel):
    """Dynamic operational parameters discovered during analysis."""

    model_config = ConfigDict(frozen=True)

    environment: str = Field(..., max_length=64)
    region: str = Field(default="default", max_length=64)
    deployment_version: str = Field(default="unknown", max_length=128)
    service: str = Field(default="", max_length=128)
    host: str = Field(default="", max_length=128)
    time_window: tuple[datetime, datetime]
    known_identifiers: dict[str, str] = Field(default_factory=dict)
    user_context: dict[str, Any] = Field(default_factory=dict)
    application_metadata: dict[str, Any] = Field(default_factory=dict)


class InvestigationTransition(BaseModel):
    """Audit log entry for investigation lifecycle transition."""

    model_config = ConfigDict(frozen=True)

    from_status: InvestigationStatus
    to_status: InvestigationStatus
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    actor: ActorType
    reason: str = Field(..., max_length=1024)


class InvestigationLimits(BaseModel):
    """Hard upper bounds enforcing platform execution budget."""

    model_config = ConfigDict(frozen=True)

    max_duration_seconds: int = Field(default=1800, ge=10)
    max_tool_calls: int = Field(default=100, ge=1)
    max_evidence_items: int = Field(default=25, ge=1)
    max_hypotheses: int = Field(default=10, ge=1)
    max_correlation_depth: int = Field(default=3, ge=1)
    max_source_files: int = Field(default=20, ge=1)
    max_database_rows: int = Field(default=100, ge=1)
    max_log_query_window_minutes: int = Field(default=60, ge=1)


class InvestigationResult(BaseModel):
    """Execution response returned by infrastructure tool adapter."""

    model_config = ConfigDict(frozen=True)

    action_id: UUID
    status: str = Field(..., max_length=64)
    payload_content_uri: str = Field(..., max_length=1024)
    summary: str = Field(..., max_length=2048)
    execution_duration_ms: float = Field(..., ge=0.0)
    retrieved_evidence: list[Evidence] = Field(default_factory=list, max_length=50)


class InvestigationActionRecord(BaseModel):
    """Audit record pairing requested action with execution result."""

    model_config = ConfigDict(frozen=True)

    action: InvestigationAction
    result: InvestigationResult | None = None
    executed_at: datetime | None = None


class Investigation(BaseModel):
    """Aggregate root governing investigation state, history, and lifecycle transitions."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    session_id: str = Field(..., max_length=128)
    application_id: str = Field(..., max_length=128)
    tenant_id: str = Field(..., max_length=128)
    request: InvestigationRequest
    context: InvestigationContext
    status: InvestigationStatus = InvestigationStatus.CREATED
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    facts: list[Fact] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    entities: list[InvestigationEntity] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    evidence_references: list[EvidenceReference] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    conclusion: InvestigationConclusion | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    version: int = Field(default=1, ge=1)
    code_issue_fingerprint: str | None = Field(
        default=None,
        max_length=128,
        description="Tenant-agnostic code-issue key linking merged sessions (Part 6 D8)",
    )

    def transition_to(
        self,
        new_status: InvestigationStatus,
        actor: ActorType,
        reason: str,
        clock: Clock = SystemClock(),  # noqa: B008 - stateless clock default, safe to share
    ) -> tuple["Investigation", InvestigationTransition]:
        """Validate and construct new immutable Investigation instance using supplied Clock."""
        valid_transitions: dict[InvestigationStatus, set[InvestigationStatus]] = {
            InvestigationStatus.CREATED: {
                InvestigationStatus.CONTEXTUALIZING,
                InvestigationStatus.INVESTIGATING,
                InvestigationStatus.PAUSED,
                InvestigationStatus.CANCELLED,
                InvestigationStatus.FAILED,
            },
            InvestigationStatus.CONTEXTUALIZING: {
                InvestigationStatus.INVESTIGATING,
                InvestigationStatus.CORRELATING,
                InvestigationStatus.HYPOTHESIZING,
                InvestigationStatus.CONCLUDING,
                InvestigationStatus.PAUSED,
                InvestigationStatus.CANCELLED,
                InvestigationStatus.FAILED,
                InvestigationStatus.AWAITING_INPUT,
            },
            InvestigationStatus.INVESTIGATING: {
                InvestigationStatus.CORRELATING,
                InvestigationStatus.HYPOTHESIZING,
                InvestigationStatus.CONCLUDING,
                InvestigationStatus.PAUSED,
                InvestigationStatus.CANCELLED,
                InvestigationStatus.FAILED,
                InvestigationStatus.AWAITING_INPUT,
            },
            InvestigationStatus.CORRELATING: {
                InvestigationStatus.HYPOTHESIZING,
                InvestigationStatus.INVESTIGATING,
                InvestigationStatus.CONCLUDING,
                InvestigationStatus.PAUSED,
                InvestigationStatus.CANCELLED,
                InvestigationStatus.FAILED,
                InvestigationStatus.AWAITING_INPUT,
            },
            InvestigationStatus.HYPOTHESIZING: {
                InvestigationStatus.VERIFYING,
                InvestigationStatus.INVESTIGATING,
                InvestigationStatus.CONCLUDING,
                InvestigationStatus.PAUSED,
                InvestigationStatus.CANCELLED,
                InvestigationStatus.FAILED,
                InvestigationStatus.AWAITING_INPUT,
            },
            InvestigationStatus.VERIFYING: {
                InvestigationStatus.CONCLUDING,
                InvestigationStatus.HYPOTHESIZING,
                InvestigationStatus.INVESTIGATING,
                InvestigationStatus.CORRELATING,
                InvestigationStatus.PAUSED,
                InvestigationStatus.CANCELLED,
                InvestigationStatus.FAILED,
                InvestigationStatus.AWAITING_INPUT,
            },
            InvestigationStatus.CONCLUDING: {
                InvestigationStatus.COMPLETED,
                InvestigationStatus.FAILED,
                InvestigationStatus.PAUSED,
                InvestigationStatus.CANCELLED,
                InvestigationStatus.AWAITING_INPUT,
            },
            InvestigationStatus.COMPLETED: {
                InvestigationStatus.INVESTIGATING,
            },
            InvestigationStatus.FAILED: {
                InvestigationStatus.INVESTIGATING,
            },
            InvestigationStatus.PAUSED: {
                InvestigationStatus.INVESTIGATING,
                InvestigationStatus.CANCELLED,
                InvestigationStatus.FAILED,
                InvestigationStatus.AWAITING_INPUT,
            },
            InvestigationStatus.AWAITING_INPUT: {
                InvestigationStatus.CONTEXTUALIZING,
                InvestigationStatus.INVESTIGATING,
                InvestigationStatus.CORRELATING,
                InvestigationStatus.HYPOTHESIZING,
                InvestigationStatus.VERIFYING,
                InvestigationStatus.CONCLUDING,
                InvestigationStatus.PAUSED,
                InvestigationStatus.CANCELLED,
                InvestigationStatus.FAILED,
            },
            InvestigationStatus.CANCELLED: set(),
        }

        allowed = valid_transitions.get(self.status, set())
        if new_status not in allowed:
            raise InvalidLifecycleTransitionException(
                f"Cannot transition investigation from {self.status.value} to {new_status.value}",
                details={
                    "investigation_id": str(self.id),
                    "current_status": self.status.value,
                    "target_status": new_status.value,
                },
            )

        now = clock.utcnow()
        transition = InvestigationTransition(
            from_status=self.status,
            to_status=new_status,
            timestamp=now,
            actor=actor,
            reason=reason,
        )

        updates: dict[str, Any] = {
            "status": new_status,
            "updated_at": now,
            "version": self.version + 1,
        }
        if self.started_at is None and self.status == InvestigationStatus.CREATED:
            updates["started_at"] = now
        if new_status in {
            InvestigationStatus.COMPLETED,
            InvestigationStatus.FAILED,
            InvestigationStatus.CANCELLED,
        }:
            updates["completed_at"] = now

        new_investigation = self.model_copy(update=updates)
        return new_investigation, transition


class InvestigationState(BaseModel):
    """In-memory serializable state snapshot for execution checkpointing."""

    model_config = ConfigDict(frozen=True)

    investigation: Investigation
    active_hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=50)
    known_facts: list[Fact] = Field(default_factory=list, max_length=200)
    known_entities: list[InvestigationEntity] = Field(default_factory=list, max_length=200)
    timeline: list[TimelineEvent] = Field(default_factory=list, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=200)
    relationships: list[EvidenceRelationship] = Field(default_factory=list, max_length=500)
    pending_questions: list[str] = Field(default_factory=list, max_length=50)
    investigation_plan: list[str] = Field(default_factory=list, max_length=50)
    tool_history: list[InvestigationActionRecord] = Field(default_factory=list, max_length=100)
    findings: list[Finding] = Field(default_factory=list, max_length=50)
