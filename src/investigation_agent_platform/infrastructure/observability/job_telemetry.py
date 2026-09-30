# src/investigation_agent_platform/infrastructure/observability/job_telemetry.py
"""Shared telemetry helpers for jobs and plugins (Part 11.3F).

All helpers use bounded-cardinality labels (kind, status, operation) — tenant
and application ids travel only in structured log fields, never as metric
labels. Security-sensitive operations additionally emit immutable audit
records through the provided audit sink.
"""

from __future__ import annotations

import logging
from typing import Any

from investigation_agent_platform.domain.common.background_job import (
    BackgroundJob,
    JobProgressEvent,
)

logger = logging.getLogger(__name__)


def _bounded_tags(
    kind: str | None = None,
    status: str | None = None,
    operation: str | None = None,
    contract_version: str | None = None,
) -> dict[str, str]:
    tags: dict[str, str] = {}
    if kind:
        tags["kind"] = kind
    if status:
        tags["status"] = status
    if operation:
        tags["operation"] = operation
    if contract_version:
        tags["contract_version"] = contract_version
    return tags


def record_job_transition(
    observability: Any | None, job: BackgroundJob, previous_status: str
) -> None:
    """Emit metric + structured log for one job status transition."""
    if observability is None:
        return
    observability.record_metric(
        "job_transition",
        1.0,
        _bounded_tags(kind=job.kind, status=job.status.value),
    )
    logger.info(
        "Background job transition",
        extra={
            "job_id": str(job.id),
            "tenant_id": job.tenant_id,
            "kind": job.kind,
            "previous_status": previous_status,
            "status": job.status.value,
            "progress": job.progress,
            "total": job.total,
        },
    )


def record_job_progress(observability: Any | None, event: JobProgressEvent) -> None:
    """Emit metric for one job progress event (bounded labels only)."""
    if observability is None:
        return
    observability.record_metric(
        "job_progress",
        float(event.progress),
        _bounded_tags(kind=event.kind, status=event.status.value),
    )


def record_quota_decision(
    observability: Any | None,
    operation: str,
    allowed: bool,
    tenant_id: str,
) -> None:
    """Emit metric for one quota check outcome."""
    if observability is None:
        return
    observability.record_metric(
        "quota_decision",
        1.0 if allowed else 0.0,
        _bounded_tags(operation=operation, status="allowed" if allowed else "rejected"),
    )
    if not allowed:
        logger.warning(
            "Quota rejected operation",
            extra={"operation": operation, "tenant_id": tenant_id},
        )


def record_credential_refresh(
    observability: Any | None, provider_id: str, tenant_id: str, refreshed: bool
) -> None:
    """Emit metric for credential cache hit/refresh (never token contents)."""
    if observability is None:
        return
    observability.record_metric(
        "credential_refresh" if refreshed else "credential_cache_hit",
        1.0,
        {"provider_id": provider_id},
    )
    logger.info(
        "Credential resolved",
        extra={"provider_id": provider_id, "tenant_id": tenant_id, "refreshed": refreshed},
    )


def record_stream_event(observability: Any | None, event: str, tenant_id: str, job_id: str) -> None:
    """Emit metric for stream lifecycle events (open/close/error)."""
    if observability is None:
        return
    observability.record_metric("stream_event", 1.0, {"event": event})
    logger.info(
        "Stream event",
        extra={"event": event, "tenant_id": tenant_id, "job_id": job_id},
    )
