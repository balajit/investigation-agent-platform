# src/investigation_agent_platform/domain/events/base.py
"""Domain events carrying tenant, correlation, and causation metadata."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class InvestigationEvent(BaseModel):
    """Base immutable domain event model with mandatory multi-tenant audit context."""

    model_config = ConfigDict(frozen=True)

    event_id: UUID = Field(default_factory=uuid4)
    event_type: str = Field(..., max_length=128, description="Fully qualified event type name")
    tenant_id: str = Field(..., max_length=128, description="Tenant scope identifier")
    investigation_id: UUID = Field(..., description="Parent investigation ID")
    application_id: str = Field(..., max_length=128, description="Application context identifier")
    correlation_id: UUID = Field(..., description="Root trace/correlation identifier")
    causation_id: UUID | None = Field(default=None, description="Direct cause event identifier")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    version: int = Field(default=1, ge=1, description="Domain event schema version")
    producer_module: str = Field(default="domain.events", max_length=128)


class InvestigationStarted(InvestigationEvent):
    event_type: str = "InvestigationStarted"
    trigger_source: str = Field(..., max_length=128)
    initial_objective: str = Field(..., max_length=2048)


class InvestigationActionProposed(InvestigationEvent):
    event_type: str = "InvestigationActionProposed"
    action_id: UUID
    capability_name: str = Field(..., max_length=128)
    reasoning_summary: str = Field(..., max_length=2048)


class InvestigationActionExecuted(InvestigationEvent):
    event_type: str = "InvestigationActionExecuted"
    action_id: UUID
    status: str = Field(..., max_length=64)
    execution_duration_ms: float = Field(..., ge=0.0)


class EvidenceDiscovered(InvestigationEvent):
    event_type: str = "EvidenceDiscovered"
    evidence_id: UUID
    evidence_type: str = Field(..., max_length=64)
    source: str = Field(..., max_length=512)


class FactEstablished(InvestigationEvent):
    event_type: str = "FactEstablished"
    fact_id: UUID
    statement: str = Field(..., max_length=2048)


class HypothesisCreated(InvestigationEvent):
    event_type: str = "HypothesisCreated"
    hypothesis_id: UUID
    title: str = Field(..., max_length=256)


class HypothesisVerified(InvestigationEvent):
    event_type: str = "HypothesisVerified"
    hypothesis_id: UUID
    confidence_score: float = Field(..., ge=0.0, le=1.0)


class ContradictionDiscovered(InvestigationEvent):
    event_type: str = "ContradictionDiscovered"
    contradiction_id: UUID
    severity: str = Field(..., max_length=32)
    statement: str = Field(..., max_length=2048)


class InvestigationConcluded(InvestigationEvent):
    event_type: str = "InvestigationConcluded"
    final_status: str = Field(..., max_length=64)
    root_cause_summary: str | None = Field(default=None, max_length=4096)
    overall_confidence: float = Field(..., ge=0.0, le=1.0)


class InvestigationCancelled(InvestigationEvent):
    event_type: str = "InvestigationCancelled"
    reason: str = Field(..., max_length=1024)
    cancelled_by: str = Field(..., max_length=128)
