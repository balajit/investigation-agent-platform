"""Health monitoring and readiness probe endpoints (Part 4, section 9.4)."""

import asyncio
import logging
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
    """Readiness probe evaluating actual availability of downstream infrastructure dependencies."""
    ctx = get_app_context()

    checks: dict[str, str] = {
        "database": "UNKNOWN",
        "temporal": "UNKNOWN",
        "broker": "UNKNOWN",
    }
    all_healthy = True

    try:
        await asyncio.wait_for(
            ctx.investigation_repo.exists(
                "health-check", UUID("00000000-0000-0000-0000-000000000000")
            ),
            timeout=1.5,
        )
        checks["database"] = "CONNECTED"
    except Exception as exc:
        logger.warning("Readiness probe database check failed: %s", exc)
        checks["database"] = "DISCONNECTED"
        all_healthy = False

    # Temporal probe: try real Client.connect if temporalio available
    try:
        temporal_connected = False
        try:
            from temporalio.client import Client  # type: ignore[import-not-found]

            # Attempt to fetch config from AppContext or env
            target = getattr(ctx, "temporal_config", None)
            if target is not None:
                host = getattr(target, "target_host", "localhost:7233")
                namespace = getattr(target, "namespace", "default")
            else:
                import os

                host = os.environ.get("IAP_TEMPORAL_HOST", "localhost:7233")
                namespace = os.environ.get("IAP_TEMPORAL_NAMESPACE", "default")
            client = await asyncio.wait_for(Client.connect(host, namespace=namespace), timeout=2.0)
            # Close if needed (no explicit close required for check)
            temporal_connected = True
        except ImportError:
            logger.info("temporalio not installed, skipping Temporal readiness check")
            temporal_connected = False
            raise
        except Exception as exc:
            logger.warning("Readiness probe Temporal check failed: %s", exc)
            raise

        checks["temporal"] = "CONNECTED" if temporal_connected else "DISCONNECTED"
        if not temporal_connected:
            all_healthy = False
    except Exception as exc:
        logger.warning("Readiness probe Temporal check failed: %s", exc)
        checks["temporal"] = "DISCONNECTED"
        all_healthy = False

    # Broker probe: check FastStream/Kafka broker via AppContext if available
    try:
        broker_available = False
        broker = (
            getattr(ctx, "broker", None)
            or getattr(ctx, "kafka_broker", None)
            or getattr(ctx, "event_publisher", None)
        )
        if broker is not None:
            # Try ping/status if broker exposes it
            ping = getattr(broker, "ping", None) or getattr(broker, "status", None)
            if callable(ping):
                try:
                    res = ping()
                    if asyncio.iscoroutine(res):
                        await asyncio.wait_for(res, timeout=1.5)
                    broker_available = True
                except Exception as exc:
                    logger.warning("Readiness probe Broker ping failed: %s", exc)
                    broker_available = False
            else:
                # Broker object exists implies configured; consider connected
                broker_available = True
        else:
            # Try to see if Kafka env configured - fallback to DISCONNECTED if no broker wired
            import os

            if os.environ.get("IAP_KAFKA_SERVERS"):
                # No broker in context but env suggests it should exist -> disconnected
                broker_available = False
            else:
                # No broker configured -> treat as disconnected for probe honesty
                broker_available = False

        checks["broker"] = "CONNECTED" if broker_available else "DISCONNECTED"
        if not broker_available:
            all_healthy = False
    except Exception as exc:
        logger.warning("Readiness probe Broker check failed: %s", exc)
        checks["broker"] = "DISCONNECTED"
        all_healthy = False

    if not all_healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "UNHEALTHY", "dependencies": checks}

    return {"status": "READY", "dependencies": checks}
