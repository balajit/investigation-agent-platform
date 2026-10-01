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


def job_topic_pattern(topic_prefix: str) -> str:
    """Regex matching every tenant job topic under one prefix (Part 11.12).

    One pattern subscriber per replica replaces per-tenant subscription:
    tenant enumeration does not exist, and registering consumers after
    broker start is unreliable. New tenants flow without restarts.
    """
    import re

    return f"^{re.escape(topic_prefix)}-jobs\\..*"


def build_job_pattern_consumer(
    broker: Any, hub: JobProgressHub, topic_prefix: str, group_id: str
) -> Any:
    """Register the single pattern subscriber for all job topics."""

    @broker.subscriber(pattern=job_topic_pattern(topic_prefix), group_id=group_id)  # type: ignore[untyped-decorator]
    async def handle_job_progress(msg: Any) -> None:
        try:
            payload = msg.model_dump() if hasattr(msg, "model_dump") else dict(msg)
            event = JobProgressEvent.model_validate(payload)
        except Exception as exc:
            logger.warning("Skipping malformed job progress event", extra={"error": str(exc)})
            return
        await hub.publish(event)

    return handle_job_progress


def job_progress_event_for(tenant_id: str, job: Any) -> JobProgressEvent:
    """Build the versioned progress event for one job row (Part 11.3C)."""
    from datetime import UTC, datetime

    stages = list(getattr(job, "stages", []) or [])
    return JobProgressEvent(
        job_id=job.id,
        tenant_id=tenant_id,
        kind=str(getattr(job, "kind", "")),
        status=job.status,
        progress=int(getattr(job, "progress", 0) or 0),
        total=int(getattr(job, "total", 0) or 0),
        stage=str(getattr(stages[-1], "name", "") or "") if stages else None,
        at=datetime.now(UTC),
    )


async def maybe_publish_job_progress(ctx: Any, tenant_id: str, job: Any) -> bool:
    """Best-effort progress publish; never fails the caller (Part 11.12).

    Returns True when the event reached the broker. Missing broker, topics,
    or Kafka outages degrade to DB snapshots + local hub only.
    """
    try:
        broker = getattr(ctx, "broker", None)
        kafka = getattr(ctx, "kafka_config", None)
        if broker is None or job is None:
            return False
        prefix = getattr(kafka, "topic_prefix", None) or "iap-"
        topic = job_topic_for(tenant_id, prefix)
        event = job_progress_event_for(tenant_id, job)
        await broker.publish(event.model_dump(mode="json"), topic=topic)
        return True
    except Exception as exc:
        logger.warning(
            "Job progress publish skipped",
            extra={"tenant_id": tenant_id, "error": str(exc)},
        )
        return False


_messaging_started = False


async def ensure_job_messaging(ctx: Any) -> bool:
    """Start the broker + pattern consumer once per process (Part 11.12).

    One subscriber per API replica with a replica-unique group (shared
    groups are prohibited: partitions would load-balance away from local
    sockets). Safe to call repeatedly; missing broker skips quietly.
    """
    global _messaging_started
    if _messaging_started:
        return True
    try:
        broker = getattr(ctx, "broker", None)
        hub = getattr(ctx, "job_hub", None)
        kafka = getattr(ctx, "kafka_config", None)
        if broker is None or hub is None:
            logger.info("Job messaging skipped (no broker/hub on context)")
            return False
        prefix = getattr(kafka, "topic_prefix", None) or "iap-"
        base_group = getattr(kafka, "consumer_group", None) or "iap-consumer-group"
        build_job_pattern_consumer(broker, hub, prefix, replica_consumer_group(base_group))
        # Kafka outages must degrade, never hang boot: broker.start() can
        # block indefinitely against an unresolvable advertised listener.
        try:
            await asyncio.wait_for(broker.start(), timeout=20)
        except Exception as exc:
            try:
                await broker.close()
            except Exception:
                pass
            logger.error(
                "Job messaging broker unreachable; continuing degraded",
                extra={"error": str(exc)},
            )
            return False
        _messaging_started = True
        logger.info(
            "Job messaging started",
            extra={"group": replica_consumer_group(base_group), "prefix": prefix},
        )
        return True
    except Exception as exc:
        logger.error(
            "Job messaging failed to start; continuing degraded", extra={"error": str(exc)}
        )
        return False
