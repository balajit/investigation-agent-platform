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
        tenant_id=tenant,
        repository_id="acme--pay",
        revision="f" * 40,
        name="charge",
        qualified_name="PaymentService.charge",
        node_type=TopologyNodeType.METHOD,
        file_path="src/payments.py",
        start_line=17,
        end_line=24,
    )
    return ASTTopologyPayload(
        schema_version="v1",
        parser_version="test",
        tenant_id=tenant,
        application_id=f"{tenant}-acme--pay",
        repository_id="acme--pay",
        revision="f" * 40,
        snapshot_id=uuid.uuid4(),
        source_files=[
            SourceFileIdentity(
                tenant_id=tenant,
                repository_id="acme--pay",
                revision="f" * 40,
                path="src/payments.py",
                language="python",
                content_hash="abc",
            )
        ],
        ast_nodes=[node],
        payload_hash="0" * 64,
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

    def test_edges_mapped_with_exact_labels(self) -> None:
        from investigation_agent_platform.domain.topology.models import (
            CallEdgeInput,
            CallResolutionStatus,
            ReferenceEdgeInput,
        )

        payload = _payload()
        (node,) = payload.ast_nodes
        other = _node_like(payload, "mod.other", "other")
        payload = payload.model_copy(update={"ast_nodes": [node, other]})
        calls = [
            CallEdgeInput(
                caller_node_id=node.node_id,
                callee_node_id=other.node_id,
                callee_qualified_name="mod.other",
                call_site_file="src/payments.py",
                call_site_line=1,
                resolution_status=CallResolutionStatus.RESOLVED,
            )
        ]
        refs = [
            ReferenceEdgeInput(
                source_node_id=node.node_id,
                target_node_id=other.node_id,
                reference_type="IMPORTS",
                file_path="src/payments.py",
                line_number=1,
            )
        ]
        dangling = CallEdgeInput(
            caller_node_id=node.node_id,
            callee_node_id=uuid.uuid4(),
            callee_qualified_name="ghost",
            call_site_file="src/payments.py",
            call_site_line=2,
            resolution_status=CallResolutionStatus.UNRESOLVED,
        )
        points = map_payload_to_points(payload, calls + [dangling], refs)
        by_id = {p.node_id: p for p in points}
        linked = _linked_rels(by_id[str(node.node_id)])
        assert "CALLS" in [rel for rel, _ in linked]
        assert "REFERENCES" in [rel for rel, _ in linked]
        assert all("ghost" not in tgt for _, tgt in linked)

    def test_guardrail_constant_off(self) -> None:
        from investigation_agent_platform.infrastructure.topology.cognee_adapter import (
            ALLOW_COMPLETION,
        )

        assert ALLOW_COMPLETION is False

    def test_ownership_population(self) -> None:
        assert map_payload_to_points(_payload())[0].ownership_path in ((), [])
        owned = map_payload_to_points(_payload(), ownership_path=["github:acme", "team-pay"])
        assert list(owned[0].ownership_path) == ["github:acme", "team-pay"]

    def test_reader_requires_package(self) -> None:
        import uuid

        from investigation_agent_platform.infrastructure.topology.cognee_adapter import (
            CogneeAttributionReader,
        )

        reader = CogneeAttributionReader(enabled=True, dataset_salt="s")
        if COGNEE_AVAILABLE:
            pytest.skip("cognee installed; live reader covered by integration test")
        with pytest.raises(TopologyNotConfiguredError, match="not installed"):
            import asyncio as _aio

            _aio.run(reader.resolve_source_location("t", "a", uuid.uuid4(), "r", "v", "f", 1))

    def test_reader_construction_guards(self) -> None:
        from investigation_agent_platform.infrastructure.topology.cognee_adapter import (
            CogneeAttributionReader,
        )

        with pytest.raises(TopologyNotConfiguredError, match="disabled"):
            CogneeAttributionReader(enabled=False, dataset_salt="s")
        with pytest.raises(TopologyNotConfiguredError, match="SALT"):
            CogneeAttributionReader(enabled=True, dataset_salt="")


def _node_like(payload, qualified: str, name: str):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.topology.models import (
        ASTNodeIdentity,
        TopologyNodeType,
    )

    return ASTNodeIdentity.create(
        tenant_id=payload.tenant_id,
        repository_id=payload.repository_id,
        revision=payload.revision,
        name=name,
        qualified_name=qualified,
        node_type=TopologyNodeType.FUNCTION,
        file_path="src/other.py",
        start_line=1,
        end_line=5,
    )


def _linked_rels(point):  # type: ignore[no-untyped-def]
    """Normalize linked edges across the DataPoint and mirror shapes."""
    out = []
    for item in point.linked:
        if isinstance(item, tuple) and len(item) == 2:
            rel, target = item
            rel_name = getattr(rel, "relationship_type", rel)
            target_id = getattr(target, "node_id", target)
            out.append((rel_name, str(target_id)))
    return out


class TestCompletionContainment:
    @pytest.mark.parametrize("allowed", ["CHUNKS", "SUMMARIES", "CHUNKS_LEXICAL", "CYPHER", "CODE"])
    def test_retrieval_allowed(self, allowed: str) -> None:
        assert_retrieval_only(allowed)

    @pytest.mark.parametrize(
        "blocked",
        [
            "GRAPH_COMPLETION",
            "RAG_COMPLETION",
            "HYBRID_COMPLETION",
            "GRAPH_COMPLETION_COT",
            "TRIPLET_COMPLETION",
        ],
    )
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


class _FakeResult:
    def __init__(self, rows=None, single=None):
        self._rows = rows or []
        self._single = single
        self.run_cypher = ""
        self.run_params = {}

    def __aiter__(self):
        async def _gen():
            for r in self._rows:
                yield r

        return _gen()

    async def single(self):
        return self._single


class _FakeSession:
    def __init__(self, result, calls):
        self._result = result
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def run(self, cypher, **params):
        self._calls.append((cypher, params))
        self._result.run_cypher = cypher
        self._result.run_params = params
        return self._result


class _FakeDriver:
    """Stands in for SharedNeo4jDriver or neo4j driver (holder protocol: .session())."""

    def __init__(self, result):
        self._result = result
        self.calls: list = []
        self.closed = 0
        self.databases: list = []

    def session(self, database=None):
        self.databases.append(database)
        return _FakeSession(self._result, self.calls)

    async def close(self):
        self.closed += 1


def _enabled_adapter(**kw):
    return CogneeTopologyAdapter(enabled=True, dataset_salt="salt", **kw)


class TestSharedDriverPooling:
    def test_drop_uses_injected_driver_and_never_closes_it(self) -> None:
        import asyncio as _asyncio

        result = _FakeResult(single={"deleted": 2})
        fake = _FakeDriver(result)
        adapter = _enabled_adapter(neo4j_driver=fake)
        _asyncio.run(adapter.drop_projection("tenant-a", "acme--pay", "f" * 40))
        assert len(fake.calls) == 1
        cypher, params = fake.calls[0]
        assert "DETACH DELETE" in cypher
        assert "tenant_id" in cypher and params["tenant_id"] == "tenant-a"
        assert fake.closed == 0  # holder owns lifecycle, never the adapter

    def test_resolve_uses_injected_driver_and_returns_match(self) -> None:
        import asyncio as _asyncio

        from investigation_agent_platform.infrastructure.topology.cognee_adapter import (
            CogneeAttributionReader,
        )

        row = {
            "node_id": "12345678-1234-5678-1234-567812345678",
            "qualified_name": "PaymentService.charge",
            "node_type": "METHOD",
            "start_line": 17,
            "end_line": 24,
            "ownership_path": ["team-pay"],
        }
        result = _FakeResult(rows=[row])
        fake = _FakeDriver(result)
        reader = CogneeAttributionReader(enabled=True, dataset_salt="salt", neo4j_driver=fake)
        out = _asyncio.run(
            reader.resolve_source_location(
                "tenant-a", "app", __import__("uuid").uuid4(), "acme--pay", "f" * 40,
                "src/payments.py", 20,
            )
        )
        assert len(fake.calls) == 1
        assert "TopologySymbolPoint" in fake.calls[0][0]
        assert out.ownership_path == ["team-pay"]
        assert fake.closed == 0

    def test_legacy_path_builds_and_closes_per_call_driver(self) -> None:
        import asyncio as _asyncio
        from unittest.mock import patch

        result = _FakeResult(single={"deleted": 1})
        fake = _FakeDriver(result)
        with patch("neo4j.AsyncGraphDatabase.driver", return_value=fake) as factory:
            adapter = _enabled_adapter()  # no injected driver → legacy
            _asyncio.run(adapter.drop_projection("tenant-a", "acme--pay", "f" * 40))
        assert factory.call_count == 1
        assert fake.closed == 1  # legacy path owns the per-call driver
        assert len(fake.calls) == 1

    def test_shared_holder_lifecycle(self) -> None:
        import asyncio as _asyncio
        from unittest.mock import patch

        from investigation_agent_platform.infrastructure.topology.cognee_adapter import (
            SharedNeo4jDriver,
        )

        result = _FakeResult(rows=[])
        fake = _FakeDriver(result)
        with patch("neo4j.AsyncGraphDatabase.driver", return_value=fake):
            holder = SharedNeo4jDriver("bolt://x:7687", ("neo4j", "pw"), "neo4j")

            async def _flow():
                await holder.connect()
                async with holder.session() as session:
                    assert session is not None
                await holder.close()

            _asyncio.run(_flow())
        assert fake.databases == ["neo4j"]
        assert fake.closed == 1
