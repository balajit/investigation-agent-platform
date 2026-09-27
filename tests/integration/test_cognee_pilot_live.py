# tests/integration/test_cognee_pilot_live.py
"""Cognee pilot live projection tests (Part 8 Track B).

Opt-in via ``IAP_RUN_CONTAINER_TESTS=1`` (mirrors test_knowledge_containers.py
and test_ingest_neo4j.py); skips cleanly without the flag or when Neo4j or the
``cognee`` package is unavailable. Requires the pilot env:
``IAP_COGNEE_ENABLED=true`` plus ``IAP_COGNEE_DATASET_SALT`` and the
``IAP_TOPOLOGY_NEO4J_*`` connection vars.
"""

from __future__ import annotations

import os
import shutil
import socket
from pathlib import Path

import pytest

RUN_CONTAINERS = os.environ.get("IAP_RUN_CONTAINER_TESTS", "0") == "1"

requires_containers = pytest.mark.skipif(
    not RUN_CONTAINERS,
    reason="Container tests opt-in only (IAP_RUN_CONTAINER_TESTS=1)",
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "github_loader"
NEO4J_URI = os.environ.get("IAP_TOPOLOGY_NEO4J_URI", "bolt://localhost:7687")


def _bolt_reachable() -> bool:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(NEO4J_URI)
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 7687), timeout=1.0):
            return True
    except OSError:
        return False


@requires_containers
@pytest.mark.asyncio
async def test_live_projection_idempotent(tmp_path: Path) -> None:
    from investigation_agent_platform.infrastructure.topology.cognee_adapter import (
        COGNEE_AVAILABLE,
        CogneeTopologyAdapter,
    )

    if not COGNEE_AVAILABLE:
        pytest.skip("cognee package not installed")
    if not _bolt_reachable():
        pytest.skip("Neo4j bolt port unreachable")
    if os.environ.get("IAP_COGNEE_ENABLED", "false").lower() != "true":
        pytest.skip("IAP_COGNEE_ENABLED=true required")
    salt = os.environ.get("IAP_COGNEE_DATASET_SALT", "")
    if not salt:
        pytest.skip("IAP_COGNEE_DATASET_SALT required")

    import importlib.util
    import sys

    script = Path(__file__).resolve().parents[2] / "scripts" / "ingest_github.py"
    spec = importlib.util.spec_from_file_location("ingest_github", script)
    assert spec is not None and spec.loader is not None
    loader = importlib.util.module_from_spec(spec)
    sys.modules["ingest_github"] = loader
    spec.loader.exec_module(loader)

    dest = tmp_path / "acme--payments"
    shutil.copytree(FIXTURES / "acme--payments", dest)
    payload, truncated = await loader.build_payload_for_repo(
        dest, "tenant-live", "app-1", "acme--pay", "l" * 40)
    assert truncated is False

    adapter = CogneeTopologyAdapter(enabled=True, dataset_salt=salt)
    first = await adapter.project_snapshot(payload)
    second = await adapter.project_snapshot(payload)
    assert first.projection_hash == second.projection_hash
    assert first.point_count > 0
    await adapter.drop_projection("tenant-live", "acme--pay", "l" * 40)
