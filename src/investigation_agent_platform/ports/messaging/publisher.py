# src/investigation_agent_platform/ports/messaging/publisher.py
"""Outbound port for publishing domain events to the messaging backbone."""

from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.events.base import InvestigationEvent


class EventEnvelope(BaseModel):
    """Standardized event wrapper enforcing tenant isolation and idempotency."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID = Field(default_factory=uuid4)
    event_type: str
    tenant_id: str
    investigation_id: UUID | None = None
    idempotency_key: str
    correlation_id: str | None = None
    schema_version: str = "1.0.0"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class EventPublisher(Protocol):
    """Publishes investigation domain events to the platform message backbone."""

    async def publish(self, envelope: EventEnvelope) -> None:
        ...

    async def publish_domain_event(self, tenant_id: str, event: InvestigationEvent) -> None:
        ...

    async def publish_batch(self, envelopes: list[EventEnvelope]) -> None:
        ...