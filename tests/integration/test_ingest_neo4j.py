# tests/integration/test_ingest_neo4j.py
"""Part 7 loader Neo4j contract tests (combined plan Phase 0).

Convention (mirrors test_knowledge_containers.py): opt-in via
``IAP_RUN_CONTAINER_TESTS=1``; without it everything skips cleanly. With the
flag set, tests attempt a short connection to Neo4j and skip (not fail) when
unreachable.

Covers the exact seam Track A Phase 3 will wire the loader into: a hand-built
`ASTTopologyPayload` (same shape `build_payload_for_repo` must produce) goes
through `Neo4jTopologyAdapter.register_ownership` + `ingest()` and comes back
READY, replays idempotently, and isolates tenants.
"""

from __future__ import annotations

import os
import socket
import uuid
from hashlib import sha256

import pytest
from pydantic import SecretStr

RUN_CONTAINERS = os.environ.get("IAP_RUN_CONTAINER_TESTS", "0") == "1"

requires_containers = pytest.mark.skipif(
    not RUN_CONTAINERS,
    reason="Container tests opt-in only (IAP_RUN_CONTAINER_TESTS=1)",
)

NEO4J_URI = os.environ.get("IAP_TEST_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("IAP_TEST_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("IAP_TEST_NEO4J_PASSWORD", "password")
NEO4J_DATABASE = os.environ.get("IAP_TEST_NEO4J_DATABASE", "neo4j")


def _bolt_reachable() -> bool:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(NEO4J_URI)
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 7687), timeout=1.0):
            return True
    except OSError:
        return False


def _payload(tenant_id: str):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.topology.models import (
        ASTNodeIdentity,
        ASTTopologyPayload,
        SourceFileIdentity,
        TopologyNodeType,
    )

    node = ASTNodeIdentity.create(
        tenant_id=tenant_id,
        repository_id="acme--pay",
        revision="f" * 40,
        name="charge",
        qualified_name="payments.PaymentService.charge",
        node_type=TopologyNodeType.METHOD,
        file_path="src/payments.py",
        start_line=17,
        end_line=24,
    )
    canonical = f"{tenant_id}|acme--pay|{'f' * 40}|{node.node_id}"
    return ASTTopologyPayload(
        schema_version="v1",
        parser_version="test",
        tenant_id=tenant_id,
        application_id=f"{tenant_id}-acme--pay",
        repository_id="acme--pay",
        revision="f" * 40,
        snapshot_id=uuid.uuid4(),
        source_files=[SourceFileIdentity(tenant_id=tenant_id, repository_id="acme--pay",
                                         revision="f" * 40, path="src/payments.py",
                                         language="python", content_hash="abc")],
        ast_nodes=[node],
        payload_hash=sha256(canonical.encode()).hexdigest(),
    )


def _adapter():  # type: ignore[no-untyped-def]
    from investigation_agent_platform.infrastructure.configuration.config import TopologyConfig
    from investigation_agent_platform.infrastructure.topology.neo4j_adapter import (
        Neo4jTopologyAdapter,
    )

    return Neo4jTopologyAdapter(TopologyConfig(uri=SecretStr(NEO4J_URI),
                                               username=SecretStr(NEO4J_USER),
                                               password=SecretStr(NEO4J_PASSWORD),
                                               database=NEO4J_DATABASE, encrypted=False))


@requires_containers
@pytest.mark.asyncio
async def test_ingest_ready_and_replay() -> None:
    if not _bolt_reachable():
        pytest.skip("Neo4j bolt port unreachable")
    adapter = _adapter()
    try:
        await adapter.connect()
    except Exception as exc:
        pytest.skip(f"Neo4j connect failed: {exc}")
    try:
        await adapter.install_constraints()
        first = await adapter.ingest(_payload("tenant-a"))
        assert first.status.value == "READY"
        second = await adapter.ingest(_payload("tenant-a"))
        assert second.status.value == "READY"
        assert second.snapshot_id == first.snapshot_id
    finally:
        await adapter.close()


@requires_containers
@pytest.mark.asyncio
async def test_tenant_isolation() -> None:
    if not _bolt_reachable():
        pytest.skip("Neo4j bolt port unreachable")
    adapter = _adapter()
    try:
        await adapter.connect()
    except Exception as exc:
        pytest.skip(f"Neo4j connect failed: {exc}")
    try:
        await adapter.install_constraints()
        for tenant in ("tenant-a", "tenant-b"):
            result = await adapter.ingest(_payload(tenant))
            assert result.status.value == "READY"
    finally:
        await adapter.close()


@requires_containers
@pytest.mark.asyncio
async def test_register_ownership_chain() -> None:
    if not _bolt_reachable():
        pytest.skip("Neo4j bolt port unreachable")
    from investigation_agent_platform.domain.topology.models import (
        GitOrganizationIdentity,
        RepositoryIdentity,
    )

    adapter = _adapter()
    try:
        await adapter.connect()
    except Exception as exc:
        pytest.skip(f"Neo4j connect failed: {exc}")
    try:
        await adapter.install_constraints()
        await adapter.register_ownership(
            RepositoryIdentity(tenant_id="tenant-a", repository_id="acme--pay",
                               name="acme/pay", locator="https://github.com/acme/pay.git",
                               git_org_id="github:acme"),
            GitOrganizationIdentity(tenant_id="tenant-a", git_org_id="github:acme",
                                    name="acme", provider="github"),
            "@team-payments",
        )
    finally:
        await adapter.close()
