# src/investigation_agent_platform/infrastructure/messaging/job_fanout.py
"""Job progress event fan-out: one Kafka subscriber per replica (Part 11.3C).

Kafka assigns each partition to a single consumer in a group, so replicas
must use replica-unique consumer groups — otherwise sockets on non-owning
replicas miss live events. Duplicate delivery across replicas is intentional:
Postgres is authoritative current state, every client gets a DB snapshot
first, and event ids make local fan-out idempotent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
from typing import Any
from uuid import UUID

from investigation_agent_platform.domain.common.background_job import JobProgressEvent
from investigation_agent_platform.ports.messaging.publisher import job_topic_for

logger = logging.getLogger(__name__)


def resolve_replica_id() -> str:
    """Stable replica identity: env override, else hostname (stable restarts)."""
    return os.environ.get("IAP_REPLICA_ID", "") or socket.gethostname() or "replica-0"


def replica_consumer_group(base_group: str, replica_id: str | None = None) -> str:
    """Replica-unique consumer group; shared groups are prohibited for jobs."""
    return f"{base_group}-{replica_id or resolve_replica_id()}"


class JobProgressHub:
    """Replica-local fan-out registry: job_id -> subscriber queues.

    Thread-safe by construction: state is guarded by a threading lock and
    delivery uses ``loop.call_soon_threadsafe``, so publishers on any thread
    (API handlers, tests, broker callbacks) can fan out to subscribers living
    on the ASGI portal loop.
    """

    def __init__(self, max_queue_size: int = 200) -> None:
        self._subscribers: dict[UUID, list[tuple[asyncio.Queue[JobProgressEvent], Any]]] = {}
        self._seen_event_ids: dict[UUID, set[UUID]] = {}
        self._max_queue_size = max_queue_size
        self._lock = threading.Lock()

    async def subscribe(self, job_id: UUID) -> asyncio.Queue[JobProgressEvent]:
        queue: asyncio.Queue[JobProgressEvent] = asyncio.Queue(maxsize=self._max_queue_size)
        try:
            loop: Any = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        with self._lock:
            self._subscribers.setdefault(job_id, []).append((queue, loop))
            self._seen_event_ids.setdefault(job_id, set())
        return queue

    async def unsubscribe(self, job_id: UUID, queue: asyncio.Queue[JobProgressEvent]) -> None:
        with self._lock:
            entries = [
                (q, loop) for (q, loop) in self._subscribers.get(job_id, []) if q is not queue
            ]
            if entries:
                self._subscribers[job_id] = entries
            else:
                self._subscribers.pop(job_id, None)
                self._seen_event_ids.pop(job_id, None)

    async def publish(self, event: JobProgressEvent) -> bool:
        """Fan out to local subscribers; False when no local subscriber exists."""
        with self._lock:
            entries = list(self._subscribers.get(event.job_id, []))
            seen = self._seen_event_ids.setdefault(event.job_id, set())
            if event.event_id in seen:
                return bool(entries)
            seen.add(event.event_id)
            if len(seen) > 1000:
                seen.clear()
                seen.add(event.event_id)
        delivered = False
        for queue, loop in entries:
            try:
                if loop is not None:
                    loop.call_soon_threadsafe(_safe_put, queue, event)
                else:
                    queue.put_nowait(event)
                delivered = True
            except RuntimeError:
                try:
                    queue.put_nowait(event)
                    delivered = True
                except asyncio.QueueFull:
                    logger.warning(
                        "Job progress subscriber queue full; dropping event",
                        extra={"job_id": str(event.job_id)},
                    )
        return delivered


def _safe_put(queue: asyncio.Queue[JobProgressEvent], event: JobProgressEvent) -> None:
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        logger.warning(
            "Job progress subscriber queue full; dropping event",
            extra={"job_id": str(event.job_id)},
        )


def build_job_event_consumer(
    broker: Any,
    hub: JobProgressHub,
    topics: list[str],
    group_id: str,
) -> Any:
    """Register FastStream subscribers pushing job events into the hub.

    One subscriber per topic: FastStream Kafka routing does not reliably fan
    out a single list-of-topics subscriber (verified against TestKafkaBroker).
    Returns the list of handlers for lifespan/test introspection. Malformed
    payloads are logged and skipped — never crash the consumer loop.
    """
    handlers: list[Any] = []
    for topic in topics:
        handlers.append(_register_topic_consumer(broker, hub, topic, group_id))
    return handlers


def _register_topic_consumer(broker: Any, hub: JobProgressHub, topic: str, group_id: str) -> Any:
    @broker.subscriber(topic, group_id=group_id)  # type: ignore[untyped-decorator]
    async def handle_job_progress(msg: Any) -> None:
        try:
            payload = msg.model_dump() if hasattr(msg, "model_dump") else dict(msg)
            event = JobProgressEvent.model_validate(payload)
        except Exception as exc:
            logger.warning("Skipping malformed job progress event", extra={"error": str(exc)})
            return
        await hub.publish(event)

    return handle_job_progress


def job_topics_for_tenant(tenant_id: str, topic_prefix: str) -> str:
    """Single job topic for one tenant (kept singular: strict ordering)."""
    return job_topic_for(tenant_id, topic_prefix)
