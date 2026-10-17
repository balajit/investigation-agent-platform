# src/investigation_agent_platform/infrastructure/knowledge/context_cache.py
"""`KnowledgeContextCachePort` backends (GEMINI_RECOS R3).

- `InMemoryKnowledgeContextCache`: process-local dict with expiry. Dev/test
  default — zero infrastructure. Matches the `_group_locks` precedent: no
  cross-replica coherence; TTL-bounded staleness only.
- `RedisKnowledgeContextCache`: dockerized Redis now (`IAP_REDIS_URL` →
  compose `redis` service), managed/external Redis later with zero code
  change (adapter pattern). Invalidation is global across replicas.

Both backends are dumb: keys arrive fully qualified from the service layer
(`iap:knctx:{tenant}:{investigation}:{fingerprint}`), values are opaque.
Redis has no RLS — tenant isolation is key hygiene (tenant-first keys) plus
backend network privacy (internal compose network, loopback publish only).
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

CACHE_KEY_PREFIX = "iap:knctx"


class InMemoryKnowledgeContextCache:
    """Process-local backend. No coherence across replicas — TTL-bounded only."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, str]] = {}

    async def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= time.monotonic():
            self._store.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        self._store[key] = (time.monotonic() + ttl_seconds, value)

    async def invalidate_investigation(self, tenant_id: str, investigation_id: str) -> None:
        prefix = f"{CACHE_KEY_PREFIX}:{tenant_id}:{investigation_id}:"
        for key in [k for k in self._store if k.startswith(prefix)]:
            self._store.pop(key, None)

    async def invalidate_tenant(self, tenant_id: str) -> None:
        prefix = f"{CACHE_KEY_PREFIX}:{tenant_id}:"
        for key in [k for k in self._store if k.startswith(prefix)]:
            self._store.pop(key, None)


class RedisKnowledgeContextCache:
    """Redis backend. Shared across replicas; invalidation is global.

    `url` points at dockerized Redis in dev (`redis://localhost:6379/0`) and
    at the managed endpoint in cloud — the only thing that changes between
    environments. Short socket timeouts: a slow cache must never cost more
    than a fraction of the Mem0/Graphiti round-trips it replaces. Callers
    fail open on any exception from this class.
    """

    def __init__(self, url: str, socket_timeout_seconds: float = 0.5) -> None:
        self._url = url
        self._socket_timeout_seconds = socket_timeout_seconds
        self._client: Any | None = None

    def _client_or_raise(self) -> Any:
        if self._client is None:
            from redis.asyncio import from_url

            self._client = from_url(self._url, socket_timeout=self._socket_timeout_seconds)
        return self._client

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:
                logger.warning("Redis cache close failed: %s", exc)

    async def get(self, key: str) -> str | None:
        client = self._client_or_raise()
        value = await client.get(key)
        if value is None:
            return None
        return value.decode("utf-8") if isinstance(value, (bytes, bytearray)) else str(value)

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        client = self._client_or_raise()
        await client.set(key, value, ex=ttl_seconds)

    async def invalidate_investigation(self, tenant_id: str, investigation_id: str) -> None:
        client = self._client_or_raise()
        pattern = f"{CACHE_KEY_PREFIX}:{tenant_id}:{investigation_id}:*"
        await self._delete_pattern(client, pattern)

    async def invalidate_tenant(self, tenant_id: str) -> None:
        client = self._client_or_raise()
        pattern = f"{CACHE_KEY_PREFIX}:{tenant_id}:*"
        await self._delete_pattern(client, pattern)

    @staticmethod
    async def _delete_pattern(client: Any, pattern: str) -> None:
        # SCAN (not KEYS): production-safe on large keyspaces. Count is a
        # hint; correctness comes from iterating to exhaustion.
        cursor: int = 0
        while True:
            cursor, keys = await client.scan(cursor=cursor, match=pattern, count=200)
            if keys:
                await client.delete(*keys)
            if cursor == 0:
                break
