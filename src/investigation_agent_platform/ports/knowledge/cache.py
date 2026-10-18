# src/investigation_agent_platform/ports/knowledge/cache.py
"""Knowledge-context cache port (GEMINI_RECOS R3).

Dumb byte-store contract: backends hold opaque values under tenant-scoped
keys with TTL. All policy — key shape, envelope versioning, fail-open,
never-cache-degraded — lives in the service layer
(`application/knowledge/retrieve.py`), never here.

Backend selection (adapter pattern): dockerized Redis now
(`IAP_REDIS_URL` → compose `redis` service), managed/external Redis later
(ElastiCache/equivalent URL) with zero code change. In-memory backend for
dev/test with no infrastructure.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class KnowledgeContextCachePort(Protocol):
    """Byte cache for serialized `KnowledgeContext` envelopes.

    Keys are fully qualified by the service layer
    (`iap:knctx:{tenant}:{investigation}:{fingerprint}`); backends MUST NOT
    interpret, parse, or log key/value contents. All methods SHOULD raise on
    backend failure — callers fail open to uncached retrieval.
    """

    async def get(self, key: str) -> str | None:
        """Return the stored value, or None on miss/expiry."""
        ...

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        """Store value with a TTL. Non-positive TTL means do-not-store."""
        ...

    async def invalidate_investigation(self, tenant_id: str, investigation_id: str) -> None:
        """Drop all cached contexts for one investigation (post-capture)."""
        ...

    async def invalidate_tenant(self, tenant_id: str) -> None:
        """Drop all cached contexts for a tenant (janitor sweep)."""
        ...
