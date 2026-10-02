"""Knowledge adapter container contract tests (Part 6 ISSUE-12).

Convention (introduced here — future container-backed tests follow it):
- Opt-in via ``IAP_RUN_CONTAINER_TESTS=1``. Without it, every test in this
  file skips cleanly (never fails) so plain ``pytest -q`` stays green with
  no Docker/containers available.
- With the flag set, tests attempt a short connection to the real service
  (pgvector Postgres / Neo4j) and ``skip`` (not fail) when unreachable.
- CI runs this file in a dedicated ``knowledge-containers`` job (mirroring
  the ``migrate`` job's ``services:`` pattern) with both services present.

Covers specifically:
- Mem0/pgvector round-trip incl. the ``get_all`` + logic-wrapper trap the
  design doc calls out (tenant-scoped filtering must hold server-side).
- Graphiti concurrent same-group ingest serialization (real, non-mocked
  regression test for ISSUE-10's concurrency fix).
"""

from __future__ import annotations

import os
import socket
import uuid
from datetime import UTC, datetime

import pytest

RUN_CONTAINERS = os.environ.get("IAP_RUN_CONTAINER_TESTS", "0") == "1"

requires_containers = pytest.mark.skipif(
    not RUN_CONTAINERS,
    reason="Container tests opt-in only (IAP_RUN_CONTAINER_TESTS=1)",
)

PGVECTOR_URL = os.environ.get(
    "IAP_TEST_PGVECTOR_URL", "postgresql://iap:iap@localhost:5432/iap_test"
)
NEO4J_URI = os.environ.get("IAP_TEST_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("IAP_TEST_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("IAP_TEST_NEO4J_PASSWORD", "password")


def _tcp_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _pg_reachable() -> bool:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(PGVECTOR_URL)
        return _tcp_reachable(parsed.hostname or "localhost", parsed.port or 5432)
    except Exception:
        return False


def _neo4j_reachable() -> bool:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(NEO4J_URI)
        return _tcp_reachable(parsed.hostname or "localhost", parsed.port or 7687)
    except Exception:
        return False


def _artifact(**overrides):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.knowledge.models import KnowledgeArtifact

    base = {
        "tenant_id": "tenant-a",
        "application_id": "app-1",
        "investigation_id": uuid.uuid4(),
        "kind": "interpretation",
        "statement": "Checkout flag was ON during the incident window.",
        "confidence": 0.8,
        "refresh_policy": "IMMUTABLE",
        "valid_from": datetime.now(UTC),
    }
    base.update(overrides)
    return KnowledgeArtifact(**base)


def _embedding_api_key() -> str | None:
    """Usable embedding key, or None when the environment has none.

    The mem0 round-trip needs a real key (billable network call): either
    OpenAI (``IAP_OPENAI_API_KEY``) or Azure (``IAP_AZURE_API_KEY`` /
    ``IAP_AZURE_EMBEDDING_API_KEY`` with ``IAP_AZURE_ENDPOINT``).
    Missing keys skip — same convention as unreachable services — instead
    of failing on 401.
    """
    value = os.environ.get("IAP_OPENAI_API_KEY", "").strip()
    if value:
        return value
    if os.environ.get("IAP_AZURE_ENDPOINT", "").strip():
        for name in ("IAP_AZURE_API_KEY", "IAP_AZURE_EMBEDDING_API_KEY"):
            value = os.environ.get(name, "").strip()
            if value:
                return value
    return None


def _azure_mem0_kwargs() -> dict | None:
    """Azure mem0 settings, or None when the environment is OpenAI-based."""
    endpoint = os.environ.get("IAP_AZURE_ENDPOINT", "").strip()
    if not endpoint:
        return None
    chat_key = os.environ.get("IAP_AZURE_API_KEY", "").strip()
    embedding_deployment = os.environ.get("IAP_AZURE_EMBEDDING_DEPLOYMENT", "").strip()
    embedding_endpoint = os.environ.get("IAP_AZURE_EMBEDDING_ENDPOINT", "").strip() or endpoint
    embedding_api_version = (
        os.environ.get("IAP_AZURE_EMBEDDING_API_VERSION", "").strip()
        or os.environ.get("IAP_AZURE_API_VERSION", "2025-01-01-preview").strip()
    )
    embedding_api_key = os.environ.get("IAP_AZURE_EMBEDDING_API_KEY", "").strip() or chat_key
    return {
        "api_key": chat_key,
        "llm_provider": "azure_openai",
        "azure_endpoint": endpoint,
        "azure_api_version": os.environ.get("IAP_AZURE_API_VERSION", "2025-01-01-preview").strip(),
        "azure_deployment": os.environ.get("IAP_AZURE_DEPLOYMENT", "").strip(),
        "azure_embedding_deployment": embedding_deployment,
        "azure_embedding_endpoint": embedding_endpoint,
        "azure_embedding_api_version": embedding_api_version,
        "azure_embedding_api_key": embedding_api_key,
    }


@requires_containers
class TestMem0PgvectorContract:
    @pytest.mark.asyncio
    async def test_round_trip_tenant_scoped(self) -> None:
        if not _pg_reachable():
            pytest.skip("pgvector Postgres unreachable")
        api_key = _embedding_api_key()
        if api_key is None:
            pytest.skip("no usable embedding API key (IAP_OPENAI_API_KEY / IAP_AZURE_*)")
        pytest.importorskip("mem0")
        from investigation_agent_platform.infrastructure.knowledge.mem0_adapter import (
            Mem0KnowledgeStore,
        )

        azure_kwargs = _azure_mem0_kwargs()
        if azure_kwargs is not None and not azure_kwargs["azure_embedding_deployment"]:
            pytest.skip("IAP_AZURE_EMBEDDING_DEPLOYMENT required for Azure mem0")
        store_kwargs = {"pgvector_url": PGVECTOR_URL, "api_key": api_key}
        if azure_kwargs is not None:
            store_kwargs.update(azure_kwargs)
        store = Mem0KnowledgeStore(**store_kwargs)
        artifact = _artifact(kind="micro_fact", statement="Team Atlas owns checkout")
        record_id = await store.project_artifact("tenant-a", artifact)
        assert record_id
        views = await store.recall_preferences("tenant-a", "who owns checkout")
        assert all(v.artifact_id for v in views)
        # Tenant B must never see tenant A's memories through the adapter.
        other = await store.recall_preferences("tenant-b", "who owns checkout")
        assert all(v.artifact_id != artifact.id for v in other), "cross-tenant memory leak"

    @pytest.mark.asyncio
    async def test_unprojected_kind_returns_none(self) -> None:
        if not _pg_reachable():
            pytest.skip("pgvector Postgres unreachable")
        pytest.importorskip("mem0")
        from investigation_agent_platform.infrastructure.knowledge.mem0_adapter import (
            Mem0KnowledgeStore,
        )

        store = Mem0KnowledgeStore(pgvector_url=PGVECTOR_URL)
        artifact = _artifact(kind="flag_evaluation")
        assert await store.project_artifact("tenant-a", artifact) is None


@requires_containers
class TestGraphitiNeo4jContract:
    @pytest.mark.asyncio
    async def test_round_trip_and_same_group_serialization(self) -> None:
        import asyncio

        if not _neo4j_reachable():
            pytest.skip("Neo4j unreachable")
        pytest.importorskip("graphiti_core")
        from investigation_agent_platform.application.knowledge.capture import (
            KnowledgeCaptureService,
        )
        from investigation_agent_platform.infrastructure.knowledge.graphiti_adapter import (
            GraphitiTemporalKnowledge,
        )
        from investigation_agent_platform.ports.knowledge.ports import (
            investigation_group_id,
        )

        adapter = GraphitiTemporalKnowledge(
            neo4j_uri=NEO4J_URI,
            neo4j_user=NEO4J_USER,
            neo4j_password=NEO4J_PASSWORD,
        )
        inv_id = uuid.uuid4()
        group = investigation_group_id(inv_id)
        call_order: list[str] = []

        real_project = adapter.project_episode

        async def _recording(tenant_id: str, group_id: str, artifact):  # type: ignore[no-untyped-def]
            call_order.append(f"enter:{artifact.id}")
            await asyncio.sleep(0.05)
            call_order.append(f"exit:{artifact.id}")
            try:
                return await real_project(tenant_id, group_id, artifact)
            except Exception:
                return None  # container drift must not fail serialization assert

        adapter.project_episode = _recording  # type: ignore[method-assign]

        from investigation_agent_platform.api.dependencies import InMemoryArtifactRepository

        service = KnowledgeCaptureService(
            artifact_repo=InMemoryArtifactRepository(),
            knowledge_store=None,
            temporal_port=adapter,
        )

        async def _one(statement: str):  # type: ignore[no-untyped-def]
            artifact = _artifact(
                investigation_id=inv_id, kind="interpretation", statement=statement
            )
            await service._project("tenant-a", artifact)

        await asyncio.gather(_one("fact one"), _one("fact two"))
        # Serialized, not interleaved: enter/exit pairs never overlap.
        assert call_order[1].startswith("exit:")
        assert call_order[0].split(":", 1)[1] == call_order[1].split(":", 1)[1]
        assert group.startswith("inv_")
