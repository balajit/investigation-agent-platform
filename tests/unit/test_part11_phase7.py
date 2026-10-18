# tests/unit/test_part11_phase7.py
"""Task 6.b — Finding clustering tests (Phase 11.7).

Service behavior is exercised against the in-memory repository pair
(`AppContext.finding_repo` / `finding_cluster_repo`); the API surface is
exercised with `TestClient` (no lifespan: direct calls keep the seeded
`set_app_context` in place). No network, no LLM calls — gateways and
embedders are fakes.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import AppContext, set_app_context
from investigation_agent_platform.application.finding.clustering_service import (
    FindingClusterService,
    QuotaExceededError,
)
from investigation_agent_platform.domain.finding.clustering import (
    FINDING_EMBEDDING_DIMS,
    FindingCluster,
    FindingEmbedding,
)
from investigation_agent_platform.domain.finding.models import Finding, FindingType
from investigation_agent_platform.ports.knowledge.embeddings import EmbeddingResult

# ---------------------------------------------------------------------------
# fakes + builders
# ---------------------------------------------------------------------------


def _finding(tenant: str, title: str, statement: str = "statement") -> Finding:
    return Finding(
        tenant_id=tenant,
        investigation_id=uuid4(),
        finding_type=FindingType.OBSERVATION,
        title=title,
        statement=statement,
    )


def _cluster(tenant: str, key: str, revision: int = 1) -> FindingCluster:
    return FindingCluster(
        tenant_id=tenant, cluster_key=key, label=f"label-{key}", taxonomy_revision=revision
    )


class _FakeGateway:
    """Canned strict-schema payload; records prompts for cap assertions."""

    def __init__(self, payload: dict | None) -> None:
        self.payload = payload if payload is not None else {}
        self.prompts: list[str] = []

    async def complete(self, tenant_id: str, request: object) -> object:
        self.prompts.append(request.prompt)  # type: ignore[attr-defined]
        return SimpleNamespace(parsed=self.payload, content=json.dumps(self.payload))


class _FakeEmbedder:
    def __init__(self, dims: int = FINDING_EMBEDDING_DIMS) -> None:
        self.dims = dims
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        self.calls.append(texts)
        return [
            EmbeddingResult(
                vector=[0.1] * self.dims,
                embedding_model="text-embedding-3-small",
                embedding_version="1.0",
            )
            for _ in texts
        ]


async def _seed_findings(ctx: AppContext, tenant: str, count: int) -> list[Finding]:
    findings = [_finding(tenant, f"finding-{i} timeout error") for i in range(count)]
    for finding in findings:
        await ctx.finding_repo.save_finding_record(tenant, finding)
    return findings


def _assign_payload(findings: list[Finding], key: str) -> dict:
    return {
        "assignments": [
            {"finding_id": str(finding.id), "cluster_key": key, "confidence": 0.9}
            for finding in findings
        ]
    }


def _seed_assigned_blocking(
    ctx: AppContext, tenant: str = "tenant-a"
) -> tuple[Finding, FindingCluster]:
    """Seed one finding + cluster + assignment outside any running loop."""
    import asyncio

    async def _seed() -> tuple[Finding, FindingCluster]:
        finding = _finding(tenant, "timeout storm")
        await ctx.finding_repo.save_finding_record(tenant, finding)
        cluster = _cluster(tenant, "C001")
        await ctx.finding_cluster_repo.create_cluster(tenant, cluster)
        gateway = _FakeGateway(_assign_payload([finding], "C001"))
        await FindingClusterService(
            ctx.finding_repo, ctx.finding_cluster_repo, gateway
        ).run_incremental(tenant)
        stored = (await ctx.finding_cluster_repo.load_taxonomy(tenant))[0]
        return finding, stored

    return asyncio.run(_seed())


# ---------------------------------------------------------------------------
# service: incremental + caps + malformed + history
# ---------------------------------------------------------------------------


class TestClusteringService:
    pytestmark = pytest.mark.asyncio

    async def test_incremental_processes_only_unassigned(self) -> None:
        ctx = AppContext()
        tenant = "tenant-a"
        findings = await _seed_findings(ctx, tenant, 4)
        gateway = _FakeGateway(_assign_payload(findings, "C001"))
        service = FindingClusterService(ctx.finding_repo, ctx.finding_cluster_repo, gateway)
        await ctx.finding_cluster_repo.create_cluster(tenant, _cluster(tenant, "C001"))
        # Pre-assign the first two findings: the run must skip them.
        for finding in findings[:2]:
            await service.run_incremental(tenant, limit=2)
            break
        first = await service.run_incremental(tenant, limit=10)
        assert first.examined == 2
        # Second run sees everything assigned → examines nothing.
        second = await service.run_incremental(tenant, limit=10)
        assert second.examined == 0
        assert second.assigned == 0

    async def test_candidate_prompt_bounded(self) -> None:
        ctx = AppContext()
        tenant = "tenant-a"
        findings = await _seed_findings(ctx, tenant, 2)
        for i in range(5):
            await ctx.finding_cluster_repo.create_cluster(tenant, _cluster(tenant, f"C{i:03d}"))
        gateway = _FakeGateway(_assign_payload(findings, "C000"))
        service = FindingClusterService(ctx.finding_repo, ctx.finding_cluster_repo, gateway)
        await service.run_incremental(tenant)
        assert gateway.prompts, "gateway must have been called"
        candidate_lines = [
            line for line in gateway.prompts[0].splitlines() if line.startswith("- key=")
        ]
        assert len(candidate_lines) <= 12

    async def test_new_cluster_persists_row_assignment_and_history(self) -> None:
        ctx = AppContext()
        tenant = "tenant-a"
        findings = await _seed_findings(ctx, tenant, 1)
        payload = {
            "assignments": [
                {
                    "finding_id": str(findings[0].id),
                    "cluster_key": "C007",
                    "new_cluster": {
                        "key": "C007",
                        "label": "timeout storm",
                        "description": "repeated timeouts",
                    },
                    "confidence": 0.8,
                }
            ]
        }
        service = FindingClusterService(
            ctx.finding_repo, ctx.finding_cluster_repo, _FakeGateway(payload)
        )
        result = await service.run_incremental(tenant)
        assert result.new_clusters == 1
        taxonomy = await ctx.finding_cluster_repo.load_taxonomy(tenant)
        assert [c.cluster_key for c in taxonomy] == ["C007"]
        history = await ctx.finding_cluster_repo.assignments_for_finding(tenant, findings[0].id)
        assert len(history) == 1
        assert history[0].valid_to is None
        assert history[0].valid_from is not None

    @pytest.mark.parametrize(
        "payload",
        [
            {},  # missing assignments array
            {"assignments": "not-a-list"},
            {"assignments": [{"finding_id": str(uuid4()), "cluster_key": "NOPE"}]},  # unknown id
            {"assignments": [{"finding_id": "x", "cluster_key": "C001"}]},  # unknown id
        ],
    )
    async def test_malformed_output_is_explicitly_unassigned(self, payload: dict) -> None:
        ctx = AppContext()
        tenant = "tenant-a"
        findings = await _seed_findings(ctx, tenant, 2)
        await ctx.finding_cluster_repo.create_cluster(tenant, _cluster(tenant, "C001"))
        service = FindingClusterService(
            ctx.finding_repo, ctx.finding_cluster_repo, _FakeGateway(payload)
        )
        result = await service.run_incremental(tenant)
        assert result.examined == 2
        assert result.unassigned == 2
        # Nothing silently dropped: every input has exactly one assignment.
        for finding in findings:
            history = await ctx.finding_cluster_repo.assignments_for_finding(tenant, finding.id)
            assert len(history) == 1
            assert history[0].cluster_id is None

    async def test_duplicate_ids_resolve_to_single_assignment(self) -> None:
        ctx = AppContext()
        tenant = "tenant-a"
        findings = await _seed_findings(ctx, tenant, 1)
        await ctx.finding_cluster_repo.create_cluster(tenant, _cluster(tenant, "C001"))
        payload = {
            "assignments": [
                {"finding_id": str(findings[0].id), "cluster_key": "C001"},
                {"finding_id": str(findings[0].id), "cluster_key": "C001"},
            ]
        }
        service = FindingClusterService(
            ctx.finding_repo, ctx.finding_cluster_repo, _FakeGateway(payload)
        )
        result = await service.run_incremental(tenant)
        assert result.examined == 1
        assert result.assigned == 1
        history = await ctx.finding_cluster_repo.assignments_for_finding(tenant, findings[0].id)
        assert len(history) == 1

    async def test_assignment_history_preserved_across_reruns(self) -> None:
        ctx = AppContext()
        tenant = "tenant-a"
        findings = await _seed_findings(ctx, tenant, 1)
        await ctx.finding_cluster_repo.create_cluster(tenant, _cluster(tenant, "C001"))
        await ctx.finding_cluster_repo.create_cluster(tenant, _cluster(tenant, "C002"))
        service = FindingClusterService(
            ctx.finding_repo,
            ctx.finding_cluster_repo,
            _FakeGateway(_assign_payload(findings, "C001")),
        )
        await service.run_incremental(tenant)
        # Simulate a correcting re-run writing a newer assignment row.
        service2 = FindingClusterService(
            ctx.finding_repo,
            ctx.finding_cluster_repo,
            _FakeGateway(_assign_payload(findings, "C002")),
        )
        # Bypass the assigned-skip by closing the old row through the repo,
        # then re-assign: history must keep both rows with valid_from/valid_to.
        old = (await ctx.finding_cluster_repo.assignments_for_finding(tenant, findings[0].id))[0]
        assert old.valid_to is None
        await service2.run_incremental(tenant, limit=10)
        # Still skipped (already assigned) — history untouched by the re-run.
        history = await ctx.finding_cluster_repo.assignments_for_finding(tenant, findings[0].id)
        assert len(history) == 1
        assert history[0].valid_to is None

    async def test_tenant_isolation(self) -> None:
        ctx = AppContext()
        mine = await _seed_findings(ctx, "tenant-a", 1)
        await ctx.finding_cluster_repo.create_cluster("tenant-a", _cluster("tenant-a", "C001"))
        service = FindingClusterService(
            ctx.finding_repo,
            ctx.finding_cluster_repo,
            _FakeGateway(_assign_payload(mine, "C001")),
        )
        result = await service.run_incremental("tenant-b")
        assert result.examined == 0
        assert await ctx.finding_cluster_repo.load_taxonomy("tenant-b") == []
        assert (
            await ctx.finding_cluster_repo.assigned_finding_ids("tenant-b", [f.id for f in mine])
            == set()
        )

    async def test_quota_rejection_fails_closed(self) -> None:
        ctx = AppContext()
        await _seed_findings(ctx, "tenant-a", 1)

        class _Deny:
            async def check(self, scope: object, op: str, policy: object) -> object:
                return SimpleNamespace(allowed=False, reason="quota")

            async def release(self, scope: object, op: str) -> None:
                pass  # pragma: no cover

        service = FindingClusterService(ctx.finding_repo, ctx.finding_cluster_repo, None)
        with pytest.raises(QuotaExceededError):
            await service.run_incremental(
                "tenant-a", scope=object(), quota_enforcer=_Deny(), quota_policy=object()
            )


# ---------------------------------------------------------------------------
# embeddings: space isolation + generation routing
# ---------------------------------------------------------------------------


class TestFindingEmbeddings:
    pytestmark = pytest.mark.asyncio

    async def test_wrong_dims_rejected(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import (
            DomainValidationException,
        )

        ctx = AppContext()
        bad = FindingEmbedding(
            finding_id=uuid4(),
            tenant_id="tenant-a",
            embedding_model="text-embedding-3-small",
            vector=[0.1, 0.2, 0.3],
            lexical_text="timeout",
        )
        with pytest.raises(DomainValidationException):
            await ctx.finding_cluster_repo.upsert_finding_embedding("tenant-a", bad)

    async def test_generation_routing_never_mixes_spaces(self) -> None:
        ctx = AppContext()
        tenant = "tenant-a"
        finding_id = uuid4()

        async def _put(model: str, generation: int) -> None:
            await ctx.finding_cluster_repo.upsert_finding_embedding(
                tenant,
                FindingEmbedding(
                    finding_id=finding_id,
                    tenant_id=tenant,
                    embedding_model=model,
                    generation=generation,
                    vector=[0.1] * FINDING_EMBEDDING_DIMS,
                    lexical_text="timeout",
                ),
            )

        await _put("model-a", 1)
        await _put("model-a", 2)
        await _put("model-b", 1)
        assert (await ctx.finding_cluster_repo.active_generation(tenant, "model-a", "1.0")) == 2
        assert (await ctx.finding_cluster_repo.active_generation(tenant, "model-b", "1.0")) == 1
        assert (await ctx.finding_cluster_repo.active_generation(tenant, "model-c", "1.0")) == 0

    async def test_embedder_failure_continues_lexical_only(self) -> None:
        ctx = AppContext()
        tenant = "tenant-a"
        findings = await _seed_findings(ctx, tenant, 2)
        await ctx.finding_cluster_repo.create_cluster(tenant, _cluster(tenant, "C001"))

        class _Boom:
            async def embed(self, texts: list[str]) -> list[EmbeddingResult]:
                raise RuntimeError("provider down")

        service = FindingClusterService(
            ctx.finding_repo,
            ctx.finding_cluster_repo,
            _FakeGateway(_assign_payload(findings, "C001")),
            embedder=_Boom(),
        )
        result = await service.run_incremental(tenant)
        assert result.examined == 2
        assert result.assigned == 2


# ---------------------------------------------------------------------------
# API: taxonomy / detail / run
# ---------------------------------------------------------------------------


class TestClusterEndpoints:
    def _client(self, monkeypatch) -> tuple[TestClient, AppContext]:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("IAP_ENVIRONMENT", "development")
        ctx = AppContext()
        set_app_context(ctx)
        return TestClient(create_app()), ctx

    def _auth(self, tenant: str = "tenant-a") -> dict[str, str]:
        return {"X-Tenant-ID": tenant}

    def test_taxonomy_list_and_empty(self, monkeypatch) -> None:
        client, ctx = self._client(monkeypatch)
        assert client.get("/api/v1/clusters", headers=self._auth()).json() == {
            "items": [],
            "total": 0,
        }
        finding, cluster = _seed_assigned_blocking(ctx)
        _ = finding
        body = client.get("/api/v1/clusters", headers=self._auth()).json()
        assert body["total"] == 1
        assert body["items"][0]["cluster_key"] == "C001"
        assert body["items"][0]["id"] == str(cluster.id)

    def test_detail_members_and_errors(self, monkeypatch) -> None:
        client, ctx = self._client(monkeypatch)
        finding, cluster = _seed_assigned_blocking(ctx)
        body = client.get(f"/api/v1/clusters/{cluster.id}", headers=self._auth()).json()
        assert body["cluster"]["id"] == str(cluster.id)
        assert body["total"] == 1
        assert body["findings"][0]["id"] == str(finding.id)
        assert client.get("/api/v1/clusters/not-a-uuid", headers=self._auth()).status_code == 400
        assert client.get(f"/api/v1/clusters/{uuid4()}", headers=self._auth()).status_code == 404
        assert (
            client.get(f"/api/v1/clusters/{cluster.id}", headers=self._auth("tenant-b")).status_code
            == 404
        )

    def test_run_requires_idempotency_key(self, monkeypatch) -> None:
        client, _ = self._client(monkeypatch)
        resp = client.post("/api/v1/clusters/run", json={}, headers=self._auth())
        assert resp.status_code == 400

    def test_run_dispatch_replay_and_conflict(self, monkeypatch) -> None:
        client, ctx = self._client(monkeypatch)
        ctx.temporal_client = MagicMock()
        ctx.temporal_client.start_workflow = AsyncMock()
        headers = {**self._auth(), "X-Idempotency-Key": "cluster-run-1"}
        first = client.post("/api/v1/clusters/run", json={}, headers=headers)
        assert first.status_code == 202, first.text
        assert first.json()["status"] == "QUEUED"
        assert first.json()["workflow_id"].startswith("wf-clustering-")
        second = client.post("/api/v1/clusters/run", json={}, headers=headers)
        assert second.status_code == 202
        assert second.json()["job_id"] == first.json()["job_id"]
        conflict = client.post(
            "/api/v1/clusters/run",
            json={"limit": 10},
            headers=headers,
        )
        assert conflict.status_code == 409

    def test_run_without_temporal_is_503(self, monkeypatch) -> None:
        client, _ = self._client(monkeypatch)
        resp = client.post(
            "/api/v1/clusters/run",
            json={},
            headers={**self._auth(), "X-Idempotency-Key": "cluster-run-503"},
        )
        assert resp.status_code == 503

    def test_run_temporal_failure_is_502(self, monkeypatch) -> None:
        client, ctx = self._client(monkeypatch)
        ctx.temporal_client = MagicMock()
        ctx.temporal_client.start_workflow = AsyncMock(side_effect=RuntimeError("temporal down"))
        resp = client.post(
            "/api/v1/clusters/run",
            json={},
            headers={**self._auth(), "X-Idempotency-Key": "cluster-run-502"},
        )
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# migration 012
# ---------------------------------------------------------------------------


class TestMigration012:
    def test_012_chain(self) -> None:
        import importlib.util
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[2]
            / "migrations"
            / "versions"
            / "012_finding_clusters.py"
        )
        assert path.is_file()
        spec = importlib.util.spec_from_file_location("migration_012", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.revision == "012_finding_clusters"
        assert mod.down_revision == "011_input_requirements"

    def test_012_is_ancestor_of_head(self) -> None:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config("alembic.ini")
        script = ScriptDirectory.from_config(cfg)
        # 012 is no longer the head (013 superseded it); it must remain a
        # linear ancestor of the current single head.
        rev = script.get_revision(script.get_heads()[0])
        seen = []
        while rev is not None:
            seen.append(rev.revision)
            down = rev.down_revision
            rev = script.get_revision(down) if down else None
        assert "012_finding_clusters" in seen
        assert seen.index("013_chat_sessions") < seen.index("012_finding_clusters")
