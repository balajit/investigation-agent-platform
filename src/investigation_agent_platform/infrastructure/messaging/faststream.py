# src/investigation_agent_platform/infrastructure/messaging/faststream.py
"""Kafka FastStream event backbone."""

import logging
from datetime import UTC, datetime
from typing import Any

from faststream import FastStream
from faststream.kafka import KafkaBroker
from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.events.base import InvestigationEvent
from investigation_agent_platform.ports.messaging.publisher import EventEnvelope, EventPublisher

logger = logging.getLogger(__name__)

INVESTIGATION_EVENTS_TOPIC = "investigation.events"
AUDIT_GROUP_ID = "investigation-audit-listeners"


class InvestigationCreatedEvent(BaseModel):
    """Payload for the investigation.events envelope."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    investigation_id: str
    tenant_id: str
    application_id: str
    schema_version: str = Field(default="1.0.0")
    timestamp: datetime
    correlation_id: str


class KafkaEventPublisher(EventPublisher):
    """Publishes domain events to the Kafka FastStream backbone."""

    def __init__(self, broker: KafkaBroker, topics: list[str] | None = None) -> None:
        self._broker = broker
        self._topics = topics or [INVESTIGATION_EVENTS_TOPIC]

    async def publish(self, envelope: EventEnvelope) -> None:
        payload = envelope.model_dump(mode="json")
        headers: dict[str, str] = {}
        if envelope.correlation_id:
            headers["correlation_id"] = envelope.correlation_id
            headers["X-Correlation-ID"] = envelope.correlation_id
        for topic in self._topics:
            if headers:
                await self._broker.publish(payload, topic=topic, headers=headers)
            else:
                await self._broker.publish(payload, topic=topic)
            logger.info("Published investigation event", extra={"context": {"topic": topic, "event_type": envelope.event_type, "tenant_id": envelope.tenant_id, "correlation_id": envelope.correlation_id}})

    async def publish_domain_event(self, tenant_id: str, event: InvestigationEvent) -> None:
        envelope = make_envelope(event)
        # Convert to generic envelope
        generic = EventEnvelope(
            event_type=event.event_type,
            tenant_id=tenant_id,
            investigation_id=event.investigation_id,
            idempotency_key=str(event.event_id),
            correlation_id=str(event.correlation_id),
            payload=event.model_dump(mode="json"),
        )
        await self.publish(generic)

    async def publish_batch(self, envelopes: list[EventEnvelope]) -> None:
        for env in envelopes:
            await self.publish(env)

    async def publish_raw(self, topic: str, payload: dict[str, Any]) -> None:
        await self._broker.publish(payload, topic=topic)


def build_faststream_app(
    broker: KafkaBroker,
    envelope_model: type[BaseModel] = InvestigationCreatedEvent,
    topic: str = INVESTIGATION_EVENTS_TOPIC,
    group_id: str = AUDIT_GROUP_ID,
) -> FastStream:
    """Builds the FastStream application with the investigation event subscriber."""

    @broker.subscriber(topic, group_id=group_id)
    async def handle_investigation_event(msg: BaseModel) -> None:
        logger.info("Received investigation event", extra={"context": {"event": msg.model_dump()}})

    return FastStream(broker)


def make_envelope(event: InvestigationEvent) -> InvestigationCreatedEvent:
    """Builds the documented audit envelope from a domain event."""
    return InvestigationCreatedEvent(
        event_id=str(getattr(event, "event_id", event.investigation_id)),
        investigation_id=str(event.investigation_id),
        tenant_id=getattr(event, "tenant_id", event.application_id),
        application_id=event.application_id,
        timestamp=getattr(event, "timestamp", None) or datetime.now(UTC),
        correlation_id=str(event.investigation_id),
    )