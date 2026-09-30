"""Health monitoring and readiness probe endpoints (Part 4, section 9.4)."""

import asyncio
import logging
import os
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Response, status

from investigation_agent_platform.api.dependencies import get_app_context

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    """Process liveness probe returning immediately if execution process is responsive."""
    return {"status": "UP"}


@router.get("/health/ready")
async def readiness(response: Response) -> dict[str, Any]:
    """Readiness probe evaluating the actual initialized production components (F-018).

    This probes the concrete client objects held on ``AppContext`` — the same
    objects the request-handling path uses — rather than constructing fresh
    connections or accepting configuration presence as a proxy for health.
    """
    ctx = get_app_context()
    environment = os.environ.get("IAP_ENVIRONMENT", "development").lower()

    checks: dict[str, str] = {
        "database": "UNKNOWN",
        "temporal": "UNKNOWN",
        "broker": "UNKNOWN",
    }
    all_healthy = True

    # Database: reject an in-memory repository outright in production (defense
    # in depth alongside the F-001 hard-fail startup check), otherwise probe
    # the actual repository the request path uses.
    from investigation_agent_platform.api.dependencies import InMemoryInvestigationRepository

    if environment == "production" and isinstance(
        ctx.investigation_repo, InMemoryInvestigationRepository
    ):
        checks["database"] = "DISCONNECTED"
        all_healthy = False
    else:
        try:
            await asyncio.wait_for(
                ctx.investigation_repo.exists(
                    "health-check", UUID("00000000-0000-0000-0000-000000000000")
                ),
                timeout=1.5,
            )
            # Buffered (dev-only) repos report BUFFERED while serving from the
            # WAL fallback so operators can see degraded state; the probe
            # itself still passes because the request path is functional.
            if getattr(ctx.investigation_repo, "primary_status", "UP") == "DOWN":
                checks["database"] = "BUFFERED"
            else:
                checks["database"] = "CONNECTED"
        except Exception as exc:
            logger.warning("Readiness probe database check failed: %s", exc)
            checks["database"] = "DISCONNECTED"
            all_healthy = False

    # Temporal: probe the already-connected production client rather than
    # opening a new connection per readiness check. In a topology where
    # Temporal is not required (e.g. an API-only deployment profile), this
    # is explicitly allowed by not requiring temporal_client to be present
    # unless IAP_REQUIRE_TEMPORAL=true.
    temporal_client = getattr(ctx, "temporal_client", None)
    require_temporal = os.environ.get(
        "IAP_REQUIRE_TEMPORAL", "true" if environment == "production" else "false"
    )
    if temporal_client is not None:
        try:
            # Lightweight liveness probe against the already-connected client's
            # underlying service stub, rather than opening a fresh connection.
            await asyncio.wait_for(temporal_client.service_client.check_health(), timeout=1.5)
            checks["temporal"] = "CONNECTED"
        except Exception as exc:
            logger.warning("Readiness probe Temporal check failed: %s", exc)
            checks["temporal"] = "DISCONNECTED"
            all_healthy = False
    elif require_temporal.lower() == "true":
        checks["temporal"] = "DISCONNECTED"
        all_healthy = False
    else:
        checks["temporal"] = "NOT_REQUIRED"

    # Broker: probe the actual configured publisher/broker object.
    broker = (
        getattr(ctx, "broker", None)
        or getattr(ctx, "kafka_broker", None)
        or getattr(ctx, "event_publisher", None)
    )
    require_broker = os.environ.get(
        "IAP_REQUIRE_BROKER", "true" if environment == "production" else "false"
    )
    if broker is not None:
        ping = getattr(broker, "ping", None) or getattr(broker, "status", None)
        try:
            if callable(ping):
                res = ping()
                if asyncio.iscoroutine(res):
                    await asyncio.wait_for(res, timeout=1.5)
            checks["broker"] = "CONNECTED"
        except Exception as exc:
            logger.warning("Readiness probe Broker ping failed: %s", exc)
            checks["broker"] = "DISCONNECTED"
            all_healthy = False
    elif require_broker.lower() == "true":
        checks["broker"] = "DISCONNECTED"
        all_healthy = False
    else:
        checks["broker"] = "NOT_REQUIRED"

    if not all_healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return _health_body("UNHEALTHY", checks)

    return _health_body("READY", checks)


def _health_body(status_value: str, checks: dict[str, str]) -> dict[str, Any]:
    # F-066: public readiness stays minimal — dependency topology is internal.
    # Full per-dependency detail only when explicitly enabled for operators.
    if os.environ.get("IAP_HEALTH_DETAIL", "minimal").lower() == "full":
        return {"status": status_value, "dependencies": checks}
    return {"status": status_value}
