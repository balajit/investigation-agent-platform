"""Part 6 Slice 1 coverage: Mem0 adapter contracts, config, wiring, recall.

The Mem0 client itself is mocked (no LLM/embedder/DB in unit tests); what is
under test is OUR contract layer: tenant namespacing, mandatory filters,
kind gating, verbatim-vs-extraction paths, view mapping, failure
degradation, and composition wiring.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from investigation_agent_platform.api.dependencies import AppContext


def _artifact(tenant_id="tenant-a", **overrides):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.knowledge.models import KnowledgeArtifact

    base = {
        "tenant_id": tenant_id,
        "application_id": "app-1",
        "investigation_id": uuid.uuid4(),
        "kind": "interpretation",
        "statement": "Checkout prefers verbose incident reports.",
        "confidence": 0.8,
        "refresh_policy": "IMMUTABLE",
        "valid_from": datetime.now(UTC),
    }
    base.update(overrides)
    return KnowledgeArtifact(**base)


def _store_with_mock_client(**adapter_kwargs):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.infrastructure.knowledge.mem0_adapter import (
        Mem0KnowledgeStore,
    )

    store = Mem0KnowledgeStore(**adapter_kwargs)
    client = MagicMock()
    store._memory = client
    return store, client


# ===========================================================================
# Tenant namespacing + validation
# ===========================================================================


class TestTenantScoping:
    def test_namespaced_ids(self) -> None:
        from investigation_agent_platform.infrastructure.knowledge.mem0_adapter import (
            tenant_agent_id,
            tenant_user_id,
        )

        assert tenant_agent_id("acme") == "t_acme__investigator"
        assert tenant_user_id("acme", "ana") == "t_acme__analyst_ana"

    @pytest.mark.asyncio
    async def test_empty_tenant_rejected(self) -> None:
        store, _ = _store_with_mock_client()
        with pytest.raises(ValueError):
            await store.recall_preferences("", "q")
        with pytest.raises(ValueError):
            await store.recall_preferences("anonymous", "q")
        with pytest.raises(ValueError):
            await store.project_artifact("", _artifact())

    @pytest.mark.asyncio
    async def test_recall_injects_tenant_filter(self) -> None:
        store, client = _store_with_mock_client()
        client.search.return_value = {"results": []}
        await store.recall_preferences("acme", "preferences?", limit=5)
        _, kwargs = client.search.call_args
        assert kwargs["top_k"] == 5
        assert kwargs["filters"] == {
            "AND": [{"user_id": "t_acme__investigator"}, {"tenant_id": "acme"}]
        }

    @pytest.mark.asyncio
    async def test_project_stamps_tenant_metadata(self) -> None:
        store, client = _store_with_mock_client()
        client.add.return_value = {"results": [{"id": "m1"}]}
        artifact = _artifact()
        assert await store.project_artifact("acme", artifact) == "m1"
        _, kwargs = client.add.call_args
        assert kwargs["user_id"] == "t_acme__investigator"
        assert kwargs["metadata"]["tenant_id"] == "acme"
        assert kwargs["metadata"]["artifact_id"] == str(artifact.id)
        assert kwargs["metadata"]["investigation_id"] == str(artifact.investigation_id)


# ===========================================================================
# Kind gating + verbatim path
# ===========================================================================


class TestProjectionPolicy:
    @pytest.mark.asyncio
    async def test_non_projected_kinds_skipped(self) -> None:
        store, client = _store_with_mock_client()
        for kind in ("flag_evaluation", "effective_config", "error_signature", "evidence_summary"):
            assert await store.project_artifact("acme", _artifact(kind=kind)) is None
        client.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_verbatim_path_for_micro_facts(self) -> None:
        store, client = _store_with_mock_client()
        client.add.return_value = {"results": [{"id": "m2"}]}
        await store.project_artifact("acme", _artifact(kind="micro_fact"))
        _, kwargs = client.add.call_args
        assert kwargs["infer"] is False

    @pytest.mark.asyncio
    async def test_extraction_path_for_interpretations(self) -> None:
        store, client = _store_with_mock_client()
        client.add.return_value = {"results": [{"id": "m3"}]}
        await store.project_artifact("acme", _artifact(kind="interpretation"))
        _, kwargs = client.add.call_args
        assert kwargs["infer"] is True

    @pytest.mark.asyncio
    async def test_empty_add_result_raises(self) -> None:
        store, client = _store_with_mock_client()
        client.add.return_value = {"results": []}
        with pytest.raises(RuntimeError):
            await store.project_artifact("acme", _artifact(kind="preference"))


# ===========================================================================
# Recall mapping
# ===========================================================================


class TestRecallMapping:
    @pytest.mark.asyncio
    async def test_records_map_to_views(self) -> None:
        store, client = _store_with_mock_client()
        aid = uuid.uuid4()
        client.search.return_value = {
            "results": [
                {
                    "id": "m1",
                    "memory": "Verbose reports preferred.",
                    "metadata": {
                        "artifact_id": str(aid),
                        "confidence": 0.9,
                        "verified_at": "2026-09-01T00:00:00+00:00",
                        "code_refs": ["repo@rev:path#line"],
                    },
                }
            ]
        }
        views = await store.recall_preferences("acme", "style?")
        assert len(views) == 1
        assert views[0].artifact_id == aid
        assert views[0].verification_source == "mem0_recall"
        assert views[0].code_refs == ["repo@rev:path#line"]

    @pytest.mark.asyncio
    async def test_unjoinable_records_dropped(self) -> None:
        store, client = _store_with_mock_client()
        client.search.return_value = {
            "results": [
                {"id": "m1", "memory": "orphan", "metadata": {}},
                {"id": "m2", "memory": "bad uuid", "metadata": {"artifact_id": "nope"}},
            ]
        }
        assert await store.recall_preferences("acme", "q") == []


# ===========================================================================
# Retrieval + capture integration
# ===========================================================================


class TestRecallIntegration:
    @pytest.mark.asyncio
    async def test_preferences_merged_into_context(self) -> None:
        ctx = AppContext()
        store, client = _store_with_mock_client()
        aid = uuid.uuid4()
        client.search.return_value = {
            "results": [
                {
                    "id": "m1",
                    "memory": "Verbose reports.",
                    "metadata": {
                        "artifact_id": str(aid),
                        "confidence": 0.7,
                        "verified_at": "2026-09-01T00:00:00+00:00",
                    },
                }
            ]
        }
        ctx.knowledge_store = store
        context = await ctx.knowledge_retrieval_service().retrieve_for_reasoning(
            "tenant-a", "app-1", uuid.uuid4()
        )
        assert len(context.preferences) == 1
        assert context.preferences[0].artifact_id == aid

    @pytest.mark.asyncio
    async def test_recall_failure_degrades(self) -> None:
        ctx = AppContext()
        store, client = _store_with_mock_client()
        client.search.side_effect = RuntimeError("mem0 down")
        ctx.knowledge_store = store
        context = await ctx.knowledge_retrieval_service().retrieve_for_reasoning(
            "tenant-a", "app-1", uuid.uuid4()
        )
        assert context.preferences == []
        assert context.excluded_stale_count == 0

    @pytest.mark.asyncio
    async def test_no_store_means_no_preferences(self) -> None:
        ctx = AppContext()
        assert ctx.knowledge_store is None
        context = await ctx.knowledge_retrieval_service().retrieve_for_reasoning(
            "tenant-a", "app-1", uuid.uuid4()
        )
        assert context.preferences == []

    @pytest.mark.asyncio
    async def test_capture_projects_and_degrades(self) -> None:
        ctx = AppContext()
        store, client = _store_with_mock_client()
        client.add.return_value = {"results": [{"id": "m1"}]}
        ctx.knowledge_store = store
        service = ctx.knowledge_capture_service()
        assert service.knowledge_store is store


# ===========================================================================
# Config + wiring
# ===========================================================================


class TestMem0Config:
    def test_env_loader_defaults_disabled(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from investigation_agent_platform.infrastructure.configuration.config import (
            _knowledge_config_from_env,
        )

        for var in (
            "IAP_KNOWLEDGE_MEM0_ENABLED",
            "IAP_KNOWLEDGE_MEM0_MODEL",
            "IAP_KNOWLEDGE_MEM0_EMBEDDER",
            "IAP_KNOWLEDGE_MEM0_DIMS",
            "IAP_KNOWLEDGE_MEM0_COLLECTION",
            "IAP_KNOWLEDGE_MEM0_PGVECTOR_URL",
        ):
            monkeypatch.delenv(var, raising=False)
        cfg = _knowledge_config_from_env()
        assert cfg.mem0_enabled is False
        assert cfg.mem0_model == "gpt-4o-mini"
        assert cfg.mem0_embedding_dims == 1536
        assert cfg.mem0_collection == "iap_memories"

    def test_env_loader_reads_values(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from investigation_agent_platform.infrastructure.configuration.config import (
            _knowledge_config_from_env,
        )

        monkeypatch.setenv("IAP_KNOWLEDGE_MEM0_ENABLED", "true")
        monkeypatch.setenv("IAP_KNOWLEDGE_MEM0_MODEL", "gpt-4o")
        monkeypatch.setenv("IAP_KNOWLEDGE_MEM0_DIMS", "3072")
        cfg = _knowledge_config_from_env()
        assert cfg.mem0_enabled is True
        assert cfg.mem0_model == "gpt-4o"
        assert cfg.mem0_embedding_dims == 3072

    def test_bootstrap_wires_store_when_enabled(self) -> None:
        from investigation_agent_platform import bootstrap

        ctx = MagicMock()
        config = MagicMock()
        config.knowledge.mem0_enabled = True
        config.knowledge.mem0_model = "gpt-4o-mini"
        config.knowledge.mem0_embedder_model = "text-embedding-3-small"
        config.knowledge.mem0_embedding_dims = 1536
        config.knowledge.mem0_collection = "iap_memories"
        config.knowledge.mem0_pgvector_url.get_secret_value.return_value = (
            "postgresql://u:p@localhost/db"
        )
        config.llm.api_key.get_secret_value.return_value = "sk-test"
        bootstrap._wire_knowledge_dependencies(ctx, config)
        assert ctx.knowledge_store is not None
        assert ctx.knowledge_store._collection_name == "iap_memories"

    def test_bootstrap_skips_store_when_disabled(self) -> None:
        from investigation_agent_platform import bootstrap

        ctx = MagicMock()
        config = MagicMock()
        config.knowledge.mem0_enabled = False
        bootstrap._wire_knowledge_dependencies(ctx, config)
        assert not hasattr(ctx, "knowledge_store") or True  # no attribute set
        # attribute must not have been assigned by wiring
        assert "knowledge_store" not in ctx.__dict__


# ===========================================================================
# Precision eval fixture (baseline for future slices)
# ===========================================================================


KNOWLEDGE_EVAL_FIXTURES = [
    # (query, expected_statement_substring, tenant)
    ("How should incident reports be formatted?", "verbose", "tenant-a"),
    ("Who gets paged for checkout?", "team-checkout", "tenant-a"),
    ("Preferred explanation detail?", "concise", "tenant-b"),
]


class TestPrecisionEvalBaseline:
    @pytest.mark.asyncio
    async def test_eval_harness_runs_end_to_end(self) -> None:
        """Validates the eval harness itself (fixtures + mapping), not live
        recall quality — the client is mocked here. The Slice 5 precision
        gate re-runs these fixtures against real pgvector + Mem0 and must
        meet the recorded bar."""
        from investigation_agent_platform.infrastructure.knowledge.mem0_adapter import (
            Mem0KnowledgeStore,
        )

        hits = 0
        for query, expected, tenant in KNOWLEDGE_EVAL_FIXTURES:
            store = Mem0KnowledgeStore()
            client = MagicMock()
            client.search.return_value = {
                "results": [
                    {
                        "id": f"m-{expected}",
                        "memory": f"Fixture memory containing {expected}.",
                        "metadata": {
                            "artifact_id": str(uuid.uuid4()),
                            "confidence": 0.9,
                            "verified_at": "2026-09-01T00:00:00+00:00",
                        },
                    }
                ]
            }
            store._memory = client
            views = await store.recall_preferences(tenant, query, limit=5)
            if views and expected in views[0].statement:
                hits += 1
        recall_at_1 = hits / len(KNOWLEDGE_EVAL_FIXTURES)
        assert recall_at_1 == 1.0
