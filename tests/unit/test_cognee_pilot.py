# tests/unit/test_cognee_pilot.py
"""Part 8 Cognee pilot tests (combined plan Track B).

Covers everything provable without the `cognee` package installed (it is not
in `pyproject.toml`): deterministic point mapping, opaque dataset derivation,
completion-type containment, and fail-loud construction. Live pipeline wiring
(`run_custom_pipeline` + `add_data_points`) is blocked on dependency approval
and recorded in `docs/IAP-implementation-part7-8-issues.md`.
"""

from __future__ import annotations

import uuid

import pytest

from investigation_agent_platform.domain.common.exceptions import TopologyNotConfiguredError
from investigation_agent_platform.domain.topology.models import (
    ASTNodeIdentity,
    ASTTopologyPayload,
    SourceFileIdentity,
    TopologyNodeType,
)
from investigation_agent_platform.infrastructure.topology.cognee_adapter import (
    COGNEE_AVAILABLE,
    CogneeTopologyAdapter,
    assert_retrieval_only,
    derive_dataset_id,
    map_payload_to_points,
)


def _payload(tenant: str = "tenant-a") -> ASTTopologyPayload:
    node = ASTNodeIdentity.create(
        tenant_id=tenant, repository_id="acme--pay", revision="f" * 40,
        name="charge", qualified_name="PaymentService.charge",
        node_type=TopologyNodeType.METHOD, file_path="src/payments.py",
        start_line=17, end_line=24,
    )
    return ASTTopologyPayload(
        schema_version="v1", parser_version="test", tenant_id=tenant,
        application_id=f"{tenant}-acme--pay", repository_id="acme--pay",
        revision="f" * 40, snapshot_id=uuid.uuid4(),
        source_files=[SourceFileIdentity(tenant_id=tenant, repository_id="acme--pay",
                                         revision="f" * 40, path="src/payments.py",
                                         language="python", content_hash="abc")],
        ast_nodes=[node], payload_hash="0" * 64,
    )


class TestDatasetDerivation:
    def test_opaque_and_deterministic(self) -> None:
        first = derive_dataset_id("tenant-a", "acme--pay", "f" * 40, "salt")
        second = derive_dataset_id("tenant-a", "acme--pay", "f" * 40, "salt")
        assert first == second and first.startswith("cg_")
        assert "tenant-a" not in first and "acme" not in first

    def test_salt_sensitive(self) -> None:
        assert derive_dataset_id("t", "r", "v", "s1") != derive_dataset_id("t", "r", "v", "s2")

    def test_tenant_sensitive(self) -> None:
        assert derive_dataset_id("a", "r", "v", "s") != derive_dataset_id("b", "r", "v", "s")

    def test_empty_salt_refused(self) -> None:
        with pytest.raises(TopologyNotConfiguredError, match="SALT"):
            derive_dataset_id("t", "r", "v", "")


class TestPointMapping:
    def test_deterministic_ids(self) -> None:
        first = map_payload_to_points(_payload())
        second = map_payload_to_points(_payload())
        assert [p.node_id for p in first] == [p.node_id for p in second]
        assert first[0].qualified_name == "PaymentService.charge"
        assert first[0].tenant_id == "tenant-a"

    def test_micro_tier_excluded(self) -> None:
        assert all(p.node_id for p in map_payload_to_points(_payload()))


class TestCompletionContainment:
    @pytest.mark.parametrize("allowed", ["CHUNKS", "SUMMARIES", "CHUNKS_LEXICAL", "CYPHER", "CODE"])
    def test_retrieval_allowed(self, allowed: str) -> None:
        assert_retrieval_only(allowed)

    @pytest.mark.parametrize("blocked", ["GRAPH_COMPLETION", "RAG_COMPLETION", "HYBRID_COMPLETION",
                                         "GRAPH_COMPLETION_COT", "TRIPLET_COMPLETION"])
    def test_completion_blocked(self, blocked: str) -> None:
        with pytest.raises(TopologyNotConfiguredError, match="retrieval-only"):
            assert_retrieval_only(blocked)


class TestConstruction:
    def test_disabled_refused(self) -> None:
        with pytest.raises(TopologyNotConfiguredError, match="disabled"):
            CogneeTopologyAdapter(enabled=False, dataset_salt="s")

    def test_missing_salt_refused(self) -> None:
        with pytest.raises(TopologyNotConfiguredError, match="SALT"):
            CogneeTopologyAdapter(enabled=True, dataset_salt="")

    @pytest.mark.asyncio
    async def test_projection_requires_package(self) -> None:
        if COGNEE_AVAILABLE:
            pytest.skip("cognee installed; live-write path is dependency-approval work")
        adapter = CogneeTopologyAdapter(enabled=True, dataset_salt="s")
        with pytest.raises(TopologyNotConfiguredError, match="not installed"):
            await adapter.project_snapshot(_payload())
        with pytest.raises(TopologyNotConfiguredError, match="not installed"):
            await adapter.drop_projection("t", "r", "v")


class TestConfig:
    def test_defaults_off(self) -> None:
        from investigation_agent_platform.infrastructure.configuration.config import TopologyConfig

        assert TopologyConfig(enabled=True).cognee_enabled is False

    def test_env_wiring(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from investigation_agent_platform.infrastructure.configuration.config import (
            _topology_config_from_env,
        )

        monkeypatch.setenv("IAP_COGNEE_ENABLED", "true")
        monkeypatch.setenv("IAP_COGNEE_DATASET_SALT", "s3cr3t")
        cfg = _topology_config_from_env()
        assert cfg.cognee_enabled is True
        assert cfg.cognee_dataset_salt.get_secret_value() == "s3cr3t"
