# src/investigation_agent_platform/api/rate_limit.py
"""Tenant-aware rate limiting / backpressure (F-060).

Sliding-window counters keyed by ``(tenant, principal, endpoint_class)`` with
separate quotas for investigation creation (expensive: workflow dispatch +
LLM) versus cheap reads. The default store is process-local and therefore a
single-replica development default — production deployments MUST inject a
shared store (Redis) implementing ``RateLimitStore`` so quotas hold across
replicas. Quota exhaustion returns 429 with ``Retry-After``; the limiter
itself fails open (availability over strictness) if the store errors.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class RateLimitStore(Protocol):
    async def allow(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        """Return ``(allowed, retry_after_seconds)`` for one event under the quota."""
        ...


class InMemoryRateLimitStore:
    """Single-process sliding-window store. Dev/test only — not shared across replicas."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def allow(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        window = self._hits[key]
        while window and window[0] <= now - window_seconds:
            window.popleft()
        if len(window) >= limit:
            retry_after = int(window[0] + window_seconds - now) + 1
            return False, max(retry_after, 1)
        window.append(now)
        return True, 0


# Endpoint classes → (requests, window_seconds). Creation is the expensive path.
ENDPOINT_QUOTAS: dict[str, tuple[int, int]] = {
    "investigation_create": (30, 60),
    "workflow_control": (60, 60),
    "default": (300, 60),
}

# Pre-auth peer buckets (AD1): a generous per-peer ceiling that floods
# cannot exhaust, keyed by transport peer — never by a header. Forwarded
# headers are not trusted (no trusted-proxy setting exists yet).
PRE_AUTH_QUOTAS: dict[str, tuple[int, int]] = {
    "investigation_create": (120, 60),
    "workflow_control": (240, 60),
    "default": (1200, 60),
}


def rate_limit_enabled(environment: str | None = None) -> bool:
    """Whether the limiter runs. Enabled by default only in production."""
    import os as _os

    env = (
        environment
        if environment is not None
        else _os.environ.get("IAP_ENVIRONMENT", "development")
    )
    default = "true" if str(env or "").strip().lower() == "production" else "false"
    return _os.environ.get("IAP_RATE_LIMIT_ENABLED", default).lower() == "true"


def endpoint_class_for_path(path: str) -> str:
    if path == "/api/v1/investigations":
        return "investigation_create"
    if path.endswith(("/start", "/cancel", "/pause", "/resume")):
        return "workflow_control"
    return "default"


def build_rate_limit_key(tenant: str | None, principal: str | None, endpoint_class: str) -> str:
    return f"{tenant or 'anonymous'}:{principal or 'anonymous'}:{endpoint_class}"


async def check_rate_limit(
    store: Any,
    tenant: str | None,
    principal: str | None,
    path: str,
    quotas: dict[str, tuple[int, int]] | None = None,
) -> tuple[bool, int, str]:
    """Return ``(allowed, retry_after, endpoint_class)``; fails open on store errors."""
    table = quotas or ENDPOINT_QUOTAS
    klass = endpoint_class_for_path(path)
    limit, window = table.get(klass, table["default"])
    key = build_rate_limit_key(tenant, principal, klass)
    try:
        allowed, retry_after = await store.allow(key, limit, window)
        return allowed, retry_after, klass
    except Exception as exc:
        logger.warning("Rate-limit store error; failing open", extra={"error": str(exc)})
        return True, 0, klass
