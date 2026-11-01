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


#: Allowlisted envelope fields for consumer logging (AD2). Only
#: identifiers, the event type, counts and timestamps — payload values
#: never ride a log line.
_LOGGABLE_FIELDS = (
    "event_id",
    "event_type",
    "investigation_id",
    "tenant_id",
    "application_id",
    "schema_version",
    "timestamp",
    "correlation_id",
)


def envelope_for_log(msg: object) -> dict[str, Any]:
    """Allowlisted envelope for consumer logging (AD2).

    Identifiers, the event type and the payload key count only; payload
    values never. Never raises: an unreadable model yields the
    ``unreadable`` marker so logging can never fail the consumer.
    """
    from investigation_agent_platform.api.errors import sanitize_extra

    # No model to dump is not an envelope at all: unreadable, like a
    # raising dump (T03 pins both). Only allowlisted keys ever leave here.
    if not hasattr(msg, "model_dump"):
        return {"unreadable": True}
    try:
        data = msg.model_dump(mode="json")
        if not isinstance(data, dict):
            return {"unreadable": True}
    except Exception:
        return {"unreadable": True}
    out: dict[str, Any] = {k: str(data[k])[:128] for k in _LOGGABLE_FIELDS if k in data}
    payload = data.get("payload")
    out["payload_keys"] = len(payload) if isinstance(payload, dict) else 0
    return sanitize_extra(out)


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
            logger.info(
                "Published investigation event",
                extra={
                    "context": {
                        "topic": topic,
                        "event_type": envelope.event_type,
                        "tenant_id": envelope.tenant_id,
                        "correlation_id": envelope.correlation_id,
                    }
                },
            )

    async def publish_domain_event(self, tenant_id: str, event: InvestigationEvent) -> None:
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

    async def publish_to_topic(self, tenant_id: str, topic: str, payload: EventEnvelope) -> None:
        """Topic-aware publish (Part 11.3C TopicEventPublisher)."""
        if payload.tenant_id != tenant_id:
            raise ValueError("Topic payload tenant does not match publish tenant.")
        await self.publish_raw(topic, payload.model_dump(mode="json"))


def log_received(msg: object) -> None:
    """Log one consumed event as its allowlisted envelope (AD2).

    Never raises: envelope extraction is total, so logging can never
    fail the consumer.
    """
    try:
        logged = envelope_for_log(msg)
    except Exception:
        logged = {"unreadable": True}
    logger.info("Received investigation event", extra={"context": {"event": logged}})


def build_faststream_app(
    broker: KafkaBroker,
    envelope_model: type[BaseModel] = InvestigationCreatedEvent,
    topic: str = INVESTIGATION_EVENTS_TOPIC,
    group_id: str = AUDIT_GROUP_ID,
) -> FastStream:
    """Builds the FastStream application with the investigation event subscriber."""

    @broker.subscriber(topic, group_id=group_id)
    async def handle_investigation_event(msg: BaseModel) -> None:
        log_received(msg)

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
