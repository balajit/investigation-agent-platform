# tests/unit/test_part11_phase8.py
"""Task 7.b — Aggregate reporting tests (Phase 11.8).

Service/renderer behavior against the in-memory repository pair; the API
surface with `TestClient` (no lifespan: direct calls keep the seeded
`set_app_context` in place). No network, no LLM calls.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import AppContext, set_app_context
from investigation_agent_platform.application.reporting.aggregate_service import (
    InvestigationAggregateReportService,
)
from investigation_agent_platform.application.reporting.artifacts import (
    history_keys,
    latest_keys,
    scope_id_for_tenant,
    store_report_run,
)
from investigation_agent_platform.domain.common.background_job import (
    BackgroundJob,
    BackgroundJobStatus,
)
from investigation_agent_platform.domain.finding.clustering import FindingCluster
from investigation_agent_platform.domain.finding.models import Finding, FindingType
from investigation_agent_platform.infrastructure.reporting.html_renderer import (
    HtmlAggregateReportRenderer,
)

# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _finding(tenant: str, title: str) -> Finding:
    return Finding(
        tenant_id=tenant,
        investigation_id=uuid4(),
        finding_type=FindingType.OBSERVATION,
        title=title,
        statement=f"{title} statement",
    )


def _cluster(tenant: str, key: str) -> FindingCluster:
    return FindingCluster(tenant_id=tenant, cluster_key=key, label=f"label-{key}")


async def _seed_taxonomy(
    ctx: AppContext, tenant: str, keys: tuple[str, ...] = ("C001", "C002")
) -> list[Finding]:
    from investigation_agent_platform.application.finding.clustering_service import (
        FindingClusterService,
    )

    findings = [_finding(tenant, f"timeout storm {key}") for key in keys]
    for finding in findings:
        await ctx.finding_repo.save_finding_record(tenant, finding)
    for key in keys:
        await ctx.finding_cluster_repo.create_cluster(tenant, _cluster(tenant, key))

    class _Gateway:
        async def complete(self, tenant_id: str, request: object) -> object:
            from types import SimpleNamespace

            prompt_keys = list(keys)
            return SimpleNamespace(
                parsed={
                    "assignments": [
                        {
                            "finding_id": str(finding.id),
                            "cluster_key": prompt_keys[i % len(prompt_keys)],
                        }
                        for i, finding in enumerate(findings)
                    ]
                },
                content="{}",
            )

    await FindingClusterService(
        ctx.finding_repo, ctx.finding_cluster_repo, _Gateway()
    ).run_incremental(tenant)
    return findings


# ---------------------------------------------------------------------------
# service: reproducibility, empty, caps, unassigned
# ---------------------------------------------------------------------------


class TestAggregateReportService:
    pytestmark = pytest.mark.asyncio

    async def test_empty_taxonomy(self) -> None:
        ctx = AppContext()
        service = InvestigationAggregateReportService(ctx.finding_cluster_repo)
        report = await service.build("tenant-a")
        assert report.empty is True
        assert report.rows == []
        assert report.taxonomy_revision == 1
        assert "taxonomy" in report.input_digests
        assert report.provenance is not None

    async def test_exact_generation_reproducibility(self) -> None:
        ctx = AppContext()
        await _seed_taxonomy(ctx, "tenant-a")
        service = InvestigationAggregateReportService(ctx.finding_cluster_repo, ctx.finding_repo)
        first = await service.build("tenant-a")
        second = await service.build("tenant-a")
        # id/generated_at (incl. provenance.generated_at) are per-build;
        # everything bound to the generation must be identical.
        first_dump = first.model_dump(mode="json")
        second_dump = second.model_dump(mode="json")
        for dump in (first_dump, second_dump):
            dump.pop("id")
            dump.pop("generated_at")
            dump["provenance"].pop("generated_at")
        assert first_dump == second_dump
        renderer = HtmlAggregateReportRenderer()
        assert renderer.render(first) == renderer.render(second)

    async def test_member_cap_truncates_explicitly(self) -> None:
        from investigation_agent_platform.domain.reporting.aggregate import (
            MAX_MEMBERS_PER_CLUSTER,
        )

        ctx = AppContext()
        tenant = "tenant-a"
        cluster = _cluster(tenant, "C001")
        await ctx.finding_cluster_repo.create_cluster(tenant, cluster)
        findings = [_finding(tenant, f"f-{i}") for i in range(MAX_MEMBERS_PER_CLUSTER + 5)]
        for finding in findings:
            await ctx.finding_repo.save_finding_record(tenant, finding)
        for finding in findings:
            from investigation_agent_platform.domain.finding.clustering import (
                FindingClusterAssignment,
            )

            await ctx.finding_cluster_repo.assign_finding(
                tenant,
                FindingClusterAssignment(
                    tenant_id=tenant, finding_id=finding.id, cluster_id=cluster.id
                ),
            )
        service = InvestigationAggregateReportService(ctx.finding_cluster_repo)
        report = await service.build(tenant)
        assert len(report.rows) == 1
        row = report.rows[0]
        assert row.member_count == MAX_MEMBERS_PER_CLUSTER + 5
        assert len(row.member_finding_ids) == MAX_MEMBERS_PER_CLUSTER
        assert row.truncated is True

    async def test_unassigned_count(self) -> None:
        ctx = AppContext()
        tenant = "tenant-a"
        await _seed_taxonomy(ctx, tenant)
        stray = _finding(tenant, "stray")
        await ctx.finding_repo.save_finding_record(tenant, stray)
        service = InvestigationAggregateReportService(ctx.finding_cluster_repo, ctx.finding_repo)
        report = await service.build(tenant)
        assert report.unassigned_count == 1
        assert report.assignment_count == 2

    async def test_rows_sorted_deterministically(self) -> None:
        ctx = AppContext()
        await _seed_taxonomy(ctx, "tenant-a", ("C002", "C001"))
        service = InvestigationAggregateReportService(ctx.finding_cluster_repo)
        report = await service.build("tenant-a")
        keys = [row.cluster_key for row in report.rows]
        assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# renderer: XSS, CSP, determinism
# ---------------------------------------------------------------------------


class TestHtmlRenderer:
    def test_malicious_text_escaped(self) -> None:
        from investigation_agent_platform.domain.reporting.aggregate import (
            AggregateReport,
            AggregateReportRow,
        )

        report = AggregateReport(
            tenant_id="tenant-a",
            taxonomy_revision=1,
            assignment_count=1,
            rows=[
                AggregateReportRow(
                    cluster_key="C001",
                    label="<script>alert('x')</script>",
                    description='<img src=x onerror="alert(1)">',
                    member_count=1,
                    member_finding_ids=[uuid4()],
                )
            ],
        )
        html = HtmlAggregateReportRenderer().render(report).decode()
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        # Description is not rendered into the table at all: unrendered
        # untrusted fields cannot become stored XSS.
        assert "<img" not in html
        assert "onerror" not in html

    def test_csp_and_no_external_refs(self) -> None:
        from investigation_agent_platform.domain.reporting.aggregate import (
            AggregateReport,
        )

        html = (
            HtmlAggregateReportRenderer()
            .render(AggregateReport(tenant_id="t", taxonomy_revision=1, empty=True))
            .decode()
        )
        assert "Content-Security-Policy" in html
        # Quotes are entity-escaped by autoescape (decoded by browsers
        # before CSP parsing); assert on the directive tokens instead.
        assert "default-src" in html
        assert "none" in html
        assert "<script" not in html
        assert "http://" not in html and "https://" not in html

    def test_empty_state(self) -> None:
        from investigation_agent_platform.domain.reporting.aggregate import (
            AggregateReport,
        )

        html = (
            HtmlAggregateReportRenderer()
            .render(AggregateReport(tenant_id="t", taxonomy_revision=1, empty=True))
            .decode()
        )
        assert "no data yet" in html.lower() or "no clustered findings" in html.lower()

    def test_render_deterministic(self) -> None:
        from investigation_agent_platform.domain.reporting.aggregate import (
            AggregateReport,
            AggregateReportRow,
        )

        report = AggregateReport(
            tenant_id="t",
            taxonomy_revision=3,
            rows=[
                AggregateReportRow(cluster_key="C001", label="a", member_count=2),
                AggregateReportRow(cluster_key="C002", label="b", member_count=1),
            ],
            input_digests={"taxonomy": "abc"},
        )
        renderer = HtmlAggregateReportRenderer()
        assert renderer.render(report) == renderer.render(report)
        assert renderer.content_type.startswith("text/html")


# ---------------------------------------------------------------------------
# registry + descriptor
# ---------------------------------------------------------------------------


class TestReportingPlugins:
    def test_default_registries_publish_renderer_and_job_kind(self) -> None:
        from investigation_agent_platform.application.extensions.registries import (
            build_default_registries,
        )

        registries = build_default_registries()
        renderer_ids = [m.plugin_id for m in registries.report_renderers.manifests()]
        assert "report-renderer:html-aggregate-v1" in renderer_ids
        job_ids = [m.plugin_id for m in registries.background_job_kinds.manifests()]
        assert "job-kind:aggregate_report" in job_ids
        impl = registries.report_renderers.get("report-renderer:html-aggregate-v1")
        assert isinstance(impl, HtmlAggregateReportRenderer)

    def test_descriptor_targets_analytics_queue(self) -> None:
        from investigation_agent_platform.application.reporting.job_kinds import (
            aggregate_report_descriptor,
        )

        descriptor = aggregate_report_descriptor()
        assert descriptor.kind == "aggregate_report"
        assert descriptor.task_queue == "analytics-tasks"
        assert descriptor.cancellable is True
        assert descriptor.retention_class == "reports"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class TestAggregateReportEndpoints:
    def _client(self, monkeypatch) -> tuple[TestClient, AppContext]:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("IAP_ENVIRONMENT", "development")
        ctx = AppContext()
        set_app_context(ctx)
        return TestClient(create_app()), ctx

    def _auth(self, tenant: str = "tenant-a") -> dict[str, str]:
        return {"X-Tenant-ID": tenant}

    def _temporal(self, ctx: AppContext) -> None:
        ctx.temporal_client = MagicMock()
        ctx.temporal_client.start_workflow = AsyncMock()

    def test_post_requires_idempotency_key(self, monkeypatch) -> None:
        client, _ = self._client(monkeypatch)
        assert (
            client.post("/api/v1/aggregate-reports", json={}, headers=self._auth()).status_code
            == 400
        )

    def test_post_dispatch_replay_conflict(self, monkeypatch) -> None:
        client, ctx = self._client(monkeypatch)
        self._temporal(ctx)
        headers = {**self._auth(), "X-Idempotency-Key": "report-1"}
        first = client.post("/api/v1/aggregate-reports", json={}, headers=headers)
        assert first.status_code == 202, first.text
        assert first.json()["status"] == "QUEUED"
        assert first.json()["workflow_id"].startswith("wf-report-")
        second = client.post("/api/v1/aggregate-reports", json={}, headers=headers)
        assert second.status_code == 202
        assert second.json()["job_id"] == first.json()["job_id"]
        conflict = client.post(
            "/api/v1/aggregate-reports", json={"legal_hold": True}, headers=headers
        )
        assert conflict.status_code == 409

    def test_post_no_temporal_503(self, monkeypatch) -> None:
        client, _ = self._client(monkeypatch)
        resp = client.post(
            "/api/v1/aggregate-reports",
            json={},
            headers={**self._auth(), "X-Idempotency-Key": "report-503"},
        )
        assert resp.status_code == 503

    def test_post_temporal_failure_502(self, monkeypatch) -> None:
        client, ctx = self._client(monkeypatch)
        ctx.temporal_client = MagicMock()
        ctx.temporal_client.start_workflow = AsyncMock(side_effect=RuntimeError("down"))
        resp = client.post(
            "/api/v1/aggregate-reports",
            json={},
            headers={**self._auth(), "X-Idempotency-Key": "report-502"},
        )
        assert resp.status_code == 502

    def test_latest_404_when_empty(self, monkeypatch) -> None:
        client, _ = self._client(monkeypatch)
        assert (
            client.get("/api/v1/aggregate-reports/latest", headers=self._auth()).status_code == 404
        )

    def test_latest_returns_signed_url(self, monkeypatch) -> None:
        import anyio

        client, ctx = self._client(monkeypatch)
        job_id = uuid4()
        anyio.run(
            store_report_run,
            ctx.artifact_store,
            "tenant-a",
            job_id,
            b"<html></html>",
            b"{}",
        )
        body = client.get("/api/v1/aggregate-reports/latest", headers=self._auth()).json()
        assert body["expires_in_seconds"] == 3600
        assert body["signed_url"]
        assert body["artifact"]["key"].startswith("aggregate-reports/latest/")
        # Opaque scope: raw tenant id must not appear in the artifact key.
        scope_id = scope_id_for_tenant("tenant-a")
        assert "tenant-a" not in body["artifact"]["key"]
        assert scope_id in body["artifact"]["key"]

    def test_cross_tenant_isolation(self, monkeypatch) -> None:
        import anyio

        client, ctx = self._client(monkeypatch)
        anyio.run(
            store_report_run, ctx.artifact_store, "tenant-a", uuid4(), b"<html></html>", b"{}"
        )
        assert (
            client.get(
                "/api/v1/aggregate-reports/latest", headers=self._auth("tenant-b")
            ).status_code
            == 404
        )
        history = client.get(
            "/api/v1/aggregate-reports/history", headers=self._auth("tenant-b")
        ).json()
        assert history["items"] == []

    def test_history_lists_runs(self, monkeypatch) -> None:
        import anyio

        client, ctx = self._client(monkeypatch)
        for _ in range(3):
            anyio.run(
                store_report_run,
                ctx.artifact_store,
                "tenant-a",
                uuid4(),
                b"<html></html>",
                b"{}",
            )
        body = client.get("/api/v1/aggregate-reports/history", headers=self._auth()).json()
        assert len(body["items"]) == 6  # html + json sidecar per run
        assert body["has_more"] is False

    def _seed_old_job(
        self, ctx: AppContext, tenant: str, days_old: int, status: BackgroundJobStatus
    ) -> UUID:
        import anyio

        job_id = uuid4()

        async def _seed() -> None:
            job = BackgroundJob(
                id=job_id,
                tenant_id=tenant,
                kind="aggregate_report",
                created_by="tester",
                created_at=datetime.now(UTC) - timedelta(days=days_old),
                status=status,
            )
            await ctx.background_job_repo.create(job)

        anyio.run(_seed)
        return job_id

    def test_purge_expired_history(self, monkeypatch) -> None:
        import anyio

        client, ctx = self._client(monkeypatch)
        old_job = self._seed_old_job(ctx, "tenant-a", 100, BackgroundJobStatus.DONE)
        fresh_job = self._seed_old_job(ctx, "tenant-a", 1, BackgroundJobStatus.DONE)
        for job_id in (old_job, fresh_job):
            anyio.run(
                store_report_run,
                ctx.artifact_store,
                "tenant-a",
                job_id,
                b"<html></html>",
                b"{}",
            )
        body = client.delete("/api/v1/aggregate-reports", headers=self._auth()).json()
        assert body["status"] == "PURGED"
        assert any(str(old_job) in key for key in body["purged"])
        assert not any(str(fresh_job) in key for key in body["purged"])

    def test_purge_keeps_active_jobs(self, monkeypatch) -> None:
        import anyio

        client, ctx = self._client(monkeypatch)
        running = self._seed_old_job(ctx, "tenant-a", 100, BackgroundJobStatus.RUNNING)
        anyio.run(
            store_report_run, ctx.artifact_store, "tenant-a", running, b"<html></html>", b"{}"
        )
        body = client.delete("/api/v1/aggregate-reports", headers=self._auth()).json()
        assert not any(str(running) in key for key in body["purged"])

    def test_purge_legal_hold_conflict(self, monkeypatch) -> None:
        import anyio

        client, ctx = self._client(monkeypatch)
        anyio.run(
            store_report_run,
            ctx.artifact_store,
            "tenant-a",
            uuid4(),
            b"<html></html>",
            b"{}",
            True,
        )
        assert client.delete("/api/v1/aggregate-reports", headers=self._auth()).status_code == 409

    def test_history_key_parsing(self) -> None:
        from investigation_agent_platform.application.reporting.artifacts import (
            job_id_from_history_key,
        )

        job_id = uuid4()
        html_key, _ = history_keys(scope_id_for_tenant("tenant-a"), job_id)
        assert job_id_from_history_key(html_key) == job_id
        assert job_id_from_history_key("aggregate-reports/latest/x") is None
        _, json_key = latest_keys(scope_id_for_tenant("tenant-a"))
        assert json_key.endswith("aggregate-report.json")
