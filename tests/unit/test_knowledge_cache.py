# tests/unit/test_knowledge_cache.py
"""R3 knowledge-context cache (GEMINI_RECOS): port contract on both backends + service policy."""

import asyncio
import time
from uuid import UUID, uuid4

import pytest

from investigation_agent_platform.application.knowledge.retrieve import (
    CACHE_FORMAT_VERSION,
    KnowledgeRetrievalService,
    cache_enabled,
    cache_key,
    cache_ttl_seconds,
)
from investigation_agent_platform.domain.knowledge.models import KnowledgeContext
from investigation_agent_platform.infrastructure.knowledge.context_cache import (
    CACHE_KEY_PREFIX,
    InMemoryKnowledgeContextCache,
    RedisKnowledgeContextCache,
)


def run(coro):
    return asyncio.run(coro)


def _backends():
    backends = [("memory", InMemoryKnowledgeContextCache())]
    try:
        from fakeredis.aioredis import FakeRedis

        redis_backend = RedisKnowledgeContextCache("redis://localhost:6379/0")
        redis_backend._client = FakeRedis()
        backends.append(("redis-fake", redis_backend))
    except ImportError:
        pass
    return backends


def _context() -> KnowledgeContext:
    return KnowledgeContext(
        verified_facts=[], preferences=[], temporal_summary=[], excluded_stale_count=0
    )


# --- port contract, both backends ---


@pytest.mark.parametrize("name,backend", _backends())
def test_set_get_roundtrip(name, backend):
    run(backend.set("k1", "v1", 60))
    assert run(backend.get("k1")) == "v1"


@pytest.mark.parametrize("name,backend", _backends())
def test_miss_returns_none(name, backend):
    assert run(backend.get("no-such-key")) is None


@pytest.mark.parametrize("name,backend", _backends())
def test_nonpositive_ttl_does_not_store(name, backend):
    run(backend.set("k0", "v", 0))
    run(backend.set("kneg", "v", -5))
    assert run(backend.get("k0")) is None
    assert run(backend.get("kneg")) is None


@pytest.mark.parametrize("name,backend", _backends())
def test_invalidate_investigation_is_scoped(name, backend):
    run(backend.set(f"{CACHE_KEY_PREFIX}:t1:inv-a:xx", "a", 60))
    run(backend.set(f"{CACHE_KEY_PREFIX}:t1:inv-b:yy", "b", 60))
    run(backend.set(f"{CACHE_KEY_PREFIX}:t2:inv-a:zz", "c", 60))
    run(backend.invalidate_investigation("t1", "inv-a"))
    assert run(backend.get(f"{CACHE_KEY_PREFIX}:t1:inv-a:xx")) is None
    assert run(backend.get(f"{CACHE_KEY_PREFIX}:t1:inv-b:yy")) == "b"
    assert run(backend.get(f"{CACHE_KEY_PREFIX}:t2:inv-a:zz")) == "c"


@pytest.mark.parametrize("name,backend", _backends())
def test_invalidate_tenant_is_scoped(name, backend):
    run(backend.set(f"{CACHE_KEY_PREFIX}:t1:inv-a:xx", "a", 60))
    run(backend.set(f"{CACHE_KEY_PREFIX}:t2:inv-a:yy", "b", 60))
    run(backend.invalidate_tenant("t1"))
    assert run(backend.get(f"{CACHE_KEY_PREFIX}:t1:inv-a:xx")) is None
    assert run(backend.get(f"{CACHE_KEY_PREFIX}:t2:inv-a:yy")) == "b"


def test_memory_backend_ttl_expiry():
    backend = InMemoryKnowledgeContextCache()
    run(backend.set("kx", "v", 1))
    assert run(backend.get("kx")) == "v"
    time.sleep(1.1)
    assert run(backend.get("kx")) is None


# --- key shape ---


def test_cache_key_tenant_first_and_covers_proofs():
    inv = uuid4()
    k1 = cache_key("t1", "app", inv, ["a"], ["fp1"])
    k2 = cache_key("t1", "app", inv, ["a"], ["fp2"])
    k3 = cache_key("t2", "app", inv, ["a"], ["fp1"])
    assert k1.startswith(f"{CACHE_KEY_PREFIX}:t1:{inv}:")
    assert len({k1, k2, k3}) == 3


def test_cache_env_defaults():
    import os

    for var in ("IAP_KNOWLEDGE_CACHE_ENABLED", "IAP_KNOWLEDGE_CACHE_TTL_SECONDS"):
        os.environ.pop(var, None)
    assert cache_enabled() is False
    assert cache_ttl_seconds() == 120


# --- service policy ---


class _StrictRepo:
    """Raises if touched: proves cache hits skip the pipeline."""

    async def list_active_for_reuse(self, *a, **k):
        raise AssertionError("repo must not be touched on cache hit")

    async def list_shared_for_fingerprint(self, *a, **k):
        raise AssertionError("repo must not be touched on cache hit")


class _EmptyRepo:
    async def list_active_for_reuse(self, tenant_id, application_id, kinds=None):
        return []

    async def list_shared_for_fingerprint(self, tenant_id, fingerprint):
        return []


class _RecordingCache(InMemoryKnowledgeContextCache):
    def __init__(self):
        super().__init__()
        self.stored: list[str] = []

    async def set(self, key, value, ttl_seconds):
        self.stored.append(key)
        await super().set(key, value, ttl_seconds)


def _service(repo, cache=None, ttl=120):
    svc = KnowledgeRetrievalService(artifact_repo=repo)
    if cache is not None:
        svc.attach_context_cache(cache, ttl)
    return svc


def test_hit_skips_pipeline():
    cache = InMemoryKnowledgeContextCache()
    svc = _service(_EmptyRepo(), cache)
    inv = uuid4()
    first = run(svc.retrieve_for_reasoning("t1", "app", inv))
    assert first.excluded_stale_count == 0
    # Second service instance, strict repo: hit must serve without touching it.
    svc2 = _service(_StrictRepo(), cache)
    second = run(svc2.retrieve_for_reasoning("t1", "app", inv))
    assert second == first


def test_miss_populates_cache():
    cache = _RecordingCache()
    svc = _service(_EmptyRepo(), cache)
    inv = uuid4()
    run(svc.retrieve_for_reasoning("t1", "app", inv))
    assert len(cache.stored) == 1
    assert cache.stored[0].startswith(f"{CACHE_KEY_PREFIX}:t1:{inv}:")


def test_ttl_zero_disables_store():
    cache = _RecordingCache()
    svc = _service(_EmptyRepo(), cache, ttl=0)
    run(svc.retrieve_for_reasoning("t1", "app", uuid4()))
    assert cache.stored == []


def test_no_cache_runs_pipeline():
    svc = _service(_EmptyRepo())
    ctx = run(svc.retrieve_for_reasoning("t1", "app", uuid4()))
    assert isinstance(ctx, KnowledgeContext)


def test_version_mismatch_is_miss():
    cache = InMemoryKnowledgeContextCache()
    svc = _service(_EmptyRepo(), cache)
    inv = uuid4()
    import json as _json

    run(cache.set(f"{CACHE_KEY_PREFIX}:t1:{inv}:x", _json.dumps({"format_version": -1}), 60))
    # Wrong key shape won't hit anyway; write a proper key with bad version.
    key = cache_key("t1", "app", inv, None, None)
    run(cache.set(key, _json.dumps({"format_version": -1, "context": {}}), 60))
    ctx = run(svc.retrieve_for_reasoning("t1", "app", inv))
    assert isinstance(ctx, KnowledgeContext)


def test_backend_failure_fails_open():
    class _BrokenCache:
        async def get(self, key):
            raise ConnectionError("redis down")

        async def set(self, key, value, ttl_seconds):
            raise ConnectionError("redis down")

    svc = _service(_EmptyRepo(), _BrokenCache())
    ctx = run(svc.retrieve_for_reasoning("t1", "app", uuid4()))
    assert isinstance(ctx, KnowledgeContext)


def test_corrupt_envelope_is_miss():
    cache = InMemoryKnowledgeContextCache()
    svc = _service(_EmptyRepo(), cache)
    inv = uuid4()
    run(cache.set(cache_key("t1", "app", inv, None, None), "not-json{{", 60))
    ctx = run(svc.retrieve_for_reasoning("t1", "app", inv))
    assert isinstance(ctx, KnowledgeContext)


def test_format_version_constant_sane():
    assert CACHE_FORMAT_VERSION >= 1


def test_redis_backend_against_fakeredis():
    pytest.importorskip("fakeredis")
    from fakeredis.aioredis import FakeRedis

    backend = RedisKnowledgeContextCache("redis://localhost:6379/0")
    backend._client = FakeRedis()
    run(backend.set("rk", "rv", 60))
    assert run(backend.get("rk")) == "rv"
    assert isinstance(UUID(str(uuid4())), UUID)
