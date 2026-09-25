# src/investigation_agent_platform/infrastructure/persistence/models.py
"""Database ORM models mapping investigation aggregate structures to PostgreSQL."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID as PyUUID
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base declarative class for all ORM models."""


class InvestigationORM(Base):
    """Database representation of Investigation aggregate root."""

    __tablename__ = "investigations"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    application_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    problem_description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    request_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    context_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    checkpoints: Mapped[list["CheckpointORM"]] = relationship(
        back_populates="investigation", cascade="all, delete-orphan", passive_deletes=True
    )
    evidence: Mapped[list["EvidenceORM"]] = relationship(
        back_populates="investigation", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        Index("idx_inv_tenant_app", "tenant_id", "application_id"),
        Index("idx_inv_tenant_status", "tenant_id", "status"),
    )


class CheckpointORM(Base):
    """Checkpoint entries for execution recovery."""

    __tablename__ = "investigation_checkpoints"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    investigation_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    current_phase: Mapped[str] = mapped_column(String(32), nullable=False)
    current_step: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    budget_state_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    last_action_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    checkpoint_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # F-025: checkpoint schema versioning — readers validate schema_version and
    # refuse to hydrate snapshots from incompatible schemas instead of blindly
    # model_validating across versions.
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    app_version: Mapped[str] = mapped_column(String(64), nullable=False, default="0.1.0")
    state_hash: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    investigation: Mapped["InvestigationORM"] = relationship(back_populates="checkpoints")

    __table_args__ = (Index("idx_checkpoint_tenant_inv", "tenant_id", "investigation_id"),)


class EvidenceORM(Base):
    """Metadata store for ingested evidence."""

    __tablename__ = "evidence"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    investigation_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(256), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    content_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)
    classification: Mapped[str] = mapped_column(String(32), nullable=False, default="INTERNAL")
    is_sanitized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    evidence_key: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False, index=True, default="")
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    investigation: Mapped["InvestigationORM"] = relationship(back_populates="evidence")

    __table_args__ = (
        Index("idx_evidence_tenant_inv", "tenant_id", "investigation_id"),
        UniqueConstraint("tenant_id", "evidence_key", name="uq_evidence_tenant_key"),
        UniqueConstraint("tenant_id", "fingerprint", name="uq_evidence_tenant_fingerprint"),
    )


class EvidenceRelationshipORM(Base):
    """Persistence table for cross-evidence relationship graph edges."""

    __tablename__ = "evidence_relationships"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    investigation_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_node_id: Mapped[str] = mapped_column(String(256), nullable=False)
    target_node_id: Mapped[str] = mapped_column(String(256), nullable=False)
    relationship_type: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    provenance_evidence_id: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_rel_src_tgt", "investigation_id", "source_node_id", "target_node_id"),
        Index("idx_rel_tenant_inv", "tenant_id", "investigation_id"),
    )


class InvestigationTransitionORM(Base):
    """Audit log entry for investigation lifecycle transitions."""

    __tablename__ = "investigation_transitions"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    investigation_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_status: Mapped[str] = mapped_column(String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), index=True
    )

    __table_args__ = (
        Index("idx_transition_inv_time", "investigation_id", "timestamp"),
        Index("idx_transition_tenant_inv", "tenant_id", "investigation_id"),
    )


class InvestigationFactORM(Base):
    """Verified fact derived from evidence during analysis."""

    __tablename__ = "investigation_facts"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    investigation_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    fact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    source_evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_fact_tenant", "tenant_id"),
        Index("idx_fact_tenant_inv", "tenant_id", "investigation_id"),
    )


class InvestigationEntityORM(Base):
    """Discovered system or business entity."""

    __tablename__ = "investigation_entities"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    investigation_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    external_identifier: Mapped[str] = mapped_column(String(256), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    source_evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    __table_args__ = (
        Index("idx_entity_tenant", "tenant_id"),
        Index("idx_entity_tenant_inv", "tenant_id", "investigation_id"),
    )


class EvidenceReferenceORM(Base):
    """Lightweight handle referencing raw evidence payload location."""

    __tablename__ = "evidence_references"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    investigation_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    evidence_id: Mapped[str] = mapped_column(String(256), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    location: Mapped[str] = mapped_column(String(512), nullable=False)
    snippet: Mapped[str] = mapped_column(Text, nullable=False, default="")


class TimelineEventORM(Base):
    """Chronologically normalized operational timeline event."""

    __tablename__ = "timeline_events"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    investigation_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    entity_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    __table_args__ = (
        Index("idx_timeline_inv_time", "investigation_id", "timestamp"),
        Index("idx_timeline_tenant", "tenant_id"),
        Index("idx_timeline_tenant_inv", "tenant_id", "investigation_id"),
    )


class HypothesisORM(Base):
    """Candidate explanation and its confidence."""

    __tablename__ = "hypotheses"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    investigation_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PROPOSED")
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    support_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    parent_hypothesis_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hypotheses.id", ondelete="SET NULL"), nullable=True
    )
    supporting_evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    refuting_evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    required_verification: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    assessments_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_hypothesis_tenant", "tenant_id"),
        Index("idx_hypothesis_tenant_inv", "tenant_id", "investigation_id"),
    )


class HypothesisEvidenceORM(Base):
    """Assessment link between evidence and hypothesis."""

    __tablename__ = "hypothesis_evidence"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    hypothesis_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("hypotheses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    evidence_id: Mapped[str] = mapped_column(String(256), nullable=False)
    assessment: Mapped[str] = mapped_column(String(32), nullable=False)
    strength: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class FindingORM(Base):
    """Validated finding / root cause report."""

    __tablename__ = "findings"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    investigation_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    finding_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False, default="MEDIUM")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    related_hypothesis_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    causal_chain: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    remediation_steps: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_finding_tenant", "tenant_id"),
        Index("idx_finding_tenant_inv", "tenant_id", "investigation_id"),
    )


class InvestigationConclusionORM(Base):
    """Terminal investigation report and root cause conclusion."""

    __tablename__ = "investigation_conclusions"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    investigation_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    supporting_evidence_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    supporting_hypothesis_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    contradicting_evidence_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    limitations: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    recommended_next_steps: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        UniqueConstraint("investigation_id", name="uq_conclusion_investigation"),
        Index("idx_conclusion_tenant", "tenant_id"),
        Index("idx_conclusion_tenant_inv", "tenant_id", "investigation_id"),
    )


class InvestigationActionORM(Base):
    """Requested action proposed by the LLM reasoner."""

    __tablename__ = "investigation_actions"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    investigation_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    execution_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class InvestigationToolExecutionORM(Base):
    """Platform action execution record pairing action with result."""

    __tablename__ = "investigation_tool_executions"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    investigation_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigation_actions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    execution_duration_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvidenceAttributeORM(Base):
    """Typed attribute key/value pairs attached to evidence items."""

    __tablename__ = "evidence_attributes"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    evidence_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("evidence.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attribute_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    attribute_value: Mapped[str] = mapped_column(Text, nullable=False)
    attribute_type: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        UniqueConstraint("evidence_id", "attribute_name", name="uq_evidence_attribute"),
    )


class AgentActionORM(Base):
    """Agent loop action execution record with request/result summaries."""

    __tablename__ = "agent_actions"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    investigation_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    tool: Mapped[str] = mapped_column(String(128), nullable=False)
    request_summary: Mapped[str] = mapped_column(Text, nullable=False)
    result_summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApplicationProfileORM(Base):
    """Onboarded application configuration profile."""

    __tablename__ = "application_profiles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    environment: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    profile_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_app_profile_tenant", "tenant_id"),
        Index("idx_app_profile_tenant_env", "tenant_id", "environment"),
    )


class IdempotencyKeyORM(Base):
    """Durable idempotency ledger (F-013/F-014): one row per (tenant, key),
    binding the stored response to a canonical hash of the originating
    request payload so a reused key with a different body is rejected
    rather than silently replaying a mismatched response."""

    __tablename__ = "idempotency_keys"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_idempotency_tenant_key"),
    )


class OutboxEventORM(Base):
    """Transactional outbox (F-058): domain events are written in the same
    transaction as the state change they describe, then dispatched and
    marked sent by a separate durable publisher process — never published
    directly from inside the request/activity transaction."""

    __tablename__ = "outbox_events"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    investigation_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), index=True
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_outbox_tenant_idempotency_key"),
        Index("idx_outbox_undispatched", "dispatched_at", "created_at"),
    )


class ActionExecutionORM(Base):
    """Durable authorization + execution audit trail for agent-proposed
    actions (F-004/F-068): every action must be traceable to an
    investigation/tenant/principal along with the authorization decision
    that permitted it."""

    __tablename__ = "action_executions"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    investigation_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    result_status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_action_exec_tenant_inv", "tenant_id", "investigation_id"),
        UniqueConstraint("action_id", name="uq_action_execution_action_id"),
    )
