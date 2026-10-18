# src/investigation_agent_platform/domain/timeline/models.py
"""Chronological timeline event models."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class TimelineEventType(StrEnum):
    REQUEST = "REQUEST"
    STATE_CHANGE = "STATE_CHANGE"
    LOG_EVENT = "LOG_EVENT"
    DATABASE_EVENT = "DATABASE_EVENT"
    EXCEPTION = "EXCEPTION"
    DEPLOYMENT = "DEPLOYMENT"
    CODE_CHANGE = "CODE_CHANGE"
    TRACE_EVENT = "TRACE_EVENT"
    UNKNOWN = "UNKNOWN"


class TimelineEvent(BaseModel):
    """Normalized operational timeline event with multi-tenant scoping and explicit sequencing."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    sequence_number: int = Field(default=0, ge=0)
    timestamp: datetime
    event_type: TimelineEventType
    description: str = Field(..., max_length=2048)
    producer: str = Field(default="system", max_length=128)
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    entity_ids: list[UUID] = Field(default_factory=list, max_length=100)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)
    attributes: dict[str, Any] = Field(default_factory=dict, max_length=50)
