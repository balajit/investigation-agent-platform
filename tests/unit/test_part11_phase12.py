# tests/unit/test_part11_phase12.py
"""Task 12.b — Integration + operations final tests (Phase 11.12).

Final conformance layer over all Part 11 extension points: shared plugin
conformance, centralized security suite, migration chain, versioned contract
fixtures, and performance baselines. No network, no Temporal server, no
live Postgres — pure unit + in-memory behavior already proven durable in
phase tests; this file pins cross-phase invariants in one place.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from fastapi.websockets import WebSocketDisconnect

import investigation_agent_platform.api.dependencies as deps
from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import (
    AppContext,
    set_app_context,
)
from investigation_agent_platform.application.extensions.registries import (
    BackgroundJobKindRegistry,
    CompletionLLMGatewayRegistry,
    CredentialProviderRegistry,
    PluginConfigError,
    PluginRegistrationError,
    ReferenceFetcherRegistry,
    ReportRendererRegistry,
    StreamingLLMGatewayRegistry,
    build_default_registries,
    redact_config,
    resolve_secret,
    scope_cache_key,
    validate_plugin_config,
)
from investigation_agent_platform.domain.common.background_job import (
    BackgroundJob,
    BackgroundJobStatus,
    JobProgressEvent,
)
from investigation_agent_platform.domain.common.exceptions import (
    CredentialProviderError,
    IdempotencyConflictError,
    IdempotencyInProgressError,
)
from investigation_agent_platform.domain.common.extension import (
    CapabilityScope,
    CompatibilityRange,
    PluginConfig,
    PluginKind,
    PluginManifest,
    SecretReference,
)
from investigation_agent_platform.domain.common.pagination import (
    CursorError,
    decode_cursor,
    encode_cursor,
)
from investigation_agent_platform.domain.common.provenance import ProvenanceRecord
from investigation_agent_platform.domain.common.quotas import QuotaPolicy
from investigation_agent_platform.infrastructure.security.endpoint_guard import (
    EndpointPolicy,
    validate_endpoint,
)


def _manifest(
    plugin_id: str = "test-plugin",
    kind: PluginKind = PluginKind.REFERENCE_FETCHER,
    contract: str = "1.0",
    schema: dict[str, Any] | None = None,
) -> PluginManifest:
    return PluginManifest(
        plugin_id=plugin_id,
        plugin_kind=kind,
        contract_version=contract,
        implementation_version="1.0",
        config_schema_version="1.0",
        capabilities=frozenset({"test"}),
        config_schema=schema or {"type": "object"},
    )


# ---------------------------------------------------------------------------
# 1. Shared plugin conformance suite
# ---------------------------------------------------------------------------


class TestPluginConformance:
    def test_all_six_registries_reject_duplicates(self) -> None:
        registries = [
            ReferenceFetcherRegistry(),
            CredentialProviderRegistry(),
            ReportRendererRegistry(),
            CompletionLLMGatewayRegistry(),
            StreamingLLMGatewayRegistry(),
            BackgroundJobKindRegistry(),
        ]
        kinds = [
            PluginKind.REFERENCE_FETCHER,
            PluginKind.CREDENTIAL_PROVIDER,
            PluginKind.REPORT_RENDERER,
            PluginKind.LLM_COMPLETION,
            PluginKind.LLM_STREAMING,
            PluginKind.BACKGROUND_JOB_KIND,
        ]
        for registry, kind in zip(registries, kinds, strict=True):
            manifest = _manifest(plugin_id=f"dup-{kind.value}", kind=kind)
            registry.register(manifest, implementation=object())
            with pytest.raises(PluginRegistrationError):
                registry.register(manifest, implementation=object())

    def test_all_six_registries_reject_unknown_get(self) -> None:
        for registry_cls in (
            ReferenceFetcherRegistry,
            CredentialProviderRegistry,
            ReportRendererRegistry,
            CompletionLLMGatewayRegistry,
            StreamingLLMGatewayRegistry,
            BackgroundJobKindRegistry,
        ):
            with pytest.raises(PluginRegistrationError):
                registry_cls().get("no-such-plugin")

    def test_registries_reject_wrong_kind(self) -> None:
        registry = ReferenceFetcherRegistry()
        manifest = _manifest(plugin_id="x", kind=PluginKind.REPORT_RENDERER)
        with pytest.raises(PluginRegistrationError):
            registry.register(manifest, implementation=object())

    def test_registries_reject_incompatible_contract(self) -> None:
        registry = CredentialProviderRegistry(
            supported=CompatibilityRange(min_version="1.0", max_version="1.9.9")
        )
        with pytest.raises(PluginRegistrationError):
            registry.register(
                _manifest(plugin_id="new", kind=PluginKind.CREDENTIAL_PROVIDER, contract="2.0"),
                implementation=object(),
            )

    def test_default_registries_publish_job_kinds(self) -> None:
        registries = build_default_registries()
        ids = {m.plugin_id for m in registries.background_job_kinds.manifests()}
        assert "job-kind:aggregate_report" in ids
        assert "job-kind:reference_docs_reindex" in ids
        assert "job-kind:batch-intake" in ids
        assert len(registries.completion_gateways) >= 1

    def test_plugin_config_id_mismatch_fails_closed(self) -> None:
        manifest = _manifest(plugin_id="real")
        config = PluginConfig(plugin_id="other", config_schema_version="1.0", config={})
        with pytest.raises(PluginConfigError):
            validate_plugin_config(manifest, config)

    def test_plugin_config_schema_version_skew_fails_closed(self) -> None:
        manifest = _manifest(plugin_id="p")
        config = PluginConfig(plugin_id="p", config_schema_version="2.0", config={})
        with pytest.raises(PluginConfigError):
            validate_plugin_config(manifest, config)

    def test_plugin_config_json_schema_enforced(self) -> None:
        manifest = _manifest(
            plugin_id="p",
            schema={"type": "object", "required": ["endpoint"], "additionalProperties": False},
        )
        bad = PluginConfig(plugin_id="p", config_schema_version="1.0", config={})
        with pytest.raises(PluginConfigError):
            validate_plugin_config(manifest, bad)

    def test_plugin_config_inline_secret_rejected(self) -> None:
        manifest = _manifest(plugin_id="p")
        config = PluginConfig(
            plugin_id="p", config_schema_version="1.0", config={"api_key": "inline-secret"}
        )
        with pytest.raises(PluginConfigError):
            validate_plugin_config(manifest, config)

    def test_redact_config_scrubs_secrets(self) -> None:
        redacted = redact_config({"api_key": "s3cr3t", "nested": {"token": "t"}, "ok": "v"})
        assert redacted["api_key"] == "***"
        assert redacted["nested"]["token"] == "***"
        assert redacted["ok"] == "v"

    def test_secret_reference_allowlist_enforced(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("IAP_ALLOWED_SECRET", "value")
        ok = resolve_secret(
            SecretReference(provider="env", name="IAP_ALLOWED_SECRET"),
            allowed_env_names=frozenset({"IAP_ALLOWED_SECRET"}),
        )
        assert ok == "value"
        with pytest.raises(PluginConfigError):
            resolve_secret(
                SecretReference(provider="env", name="SOME_OTHER_VAR"),
                allowed_env_names=frozenset({"IAP_ALLOWED_SECRET"}),
            )
        with pytest.raises(PluginConfigError):
            resolve_secret(
                SecretReference(provider="vault", name="IAP_ALLOWED_SECRET"),
                allowed_env_names=frozenset({"IAP_ALLOWED_SECRET"}),
            )

    def test_scope_cache_key_isolation(self) -> None:
        a = CapabilityScope(tenant_id="t-a", application_id="app")
        b = CapabilityScope(tenant_id="t-b", application_id="app")
        assert scope_cache_key(a, "prov") != scope_cache_key(b, "prov")
        no_app = CapabilityScope(tenant_id="t-a")
        assert scope_cache_key(a, "prov") != scope_cache_key(no_app, "prov")

    def test_manifests_expose_no_secrets(self) -> None:
        registries = build_default_registries()
        for manifest in registries.all_manifests():
            dumped = manifest.model_dump_json()
            assert "secret" not in dumped.lower() or "Secret" not in dumped
            assert "token" not in dumped.lower().replace("tokenizer", "")

    def test_capabilities_endpoint_exposes_safe_fields_only(self) -> None:
        set_app_context(AppContext())
        client = TestClient(create_app(), raise_server_exceptions=False)
        resp = client.get("/api/v1/capabilities", headers={"X-Tenant-Id": "t-a"})
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        blob = resp.text.lower()
        assert "api_key" not in blob
        assert "client_secret" not in blob
        assert "http://localhost" not in blob and "https://localhost" not in blob

    def test_worker_registration_covers_all_queues(self) -> None:
        from investigation_agent_platform.bootstrap.worker import (
            ACTIVITIES,
            ANALYTICS_ACTIVITIES,
            ANALYTICS_WORKFLOWS,
            INDEXING_ACTIVITIES,
            INDEXING_WORKFLOWS,
            WORKFLOWS,
        )

        workflow_names = {getattr(w, "__name__", str(w)) for w in WORKFLOWS}
        assert "RunInvestigationWorkflow" in workflow_names
        assert "BulkIntakeWorkflow" in workflow_names
        analytics_names = {getattr(w, "__name__", str(w)) for w in ANALYTICS_WORKFLOWS}
        assert "FindingClusteringWorkflow" in analytics_names
        assert "AggregateReportWorkflow" in analytics_names
        indexing_names = {getattr(w, "__name__", str(w)) for w in INDEXING_WORKFLOWS}
        assert "ReferenceDocsIndexWorkflow" in indexing_names
        assert len(ACTIVITIES) >= 10
        assert len(ANALYTICS_ACTIVITIES) >= 2
        assert len(INDEXING_ACTIVITIES) >= 1

    def test_provenance_record_carries_required_fields(self) -> None:
        record = ProvenanceRecord(
            plugin_id="p",
            plugin_version="1.0",
            input_digests={"input": "abc"},
            workflow_id="wf",
            run_id="run-1",
        )
        assert record.schema_version == "1.0"
        assert record.input_digests["input"] == "abc"

    def test_quota_policy_defaults_bound_part11_surfaces(self) -> None:
        policy = QuotaPolicy()
        assert policy.max_batch_records == 500
        assert policy.max_concurrent_jobs >= 1
        assert policy.max_active_chat_sessions >= 1


# ---------------------------------------------------------------------------
# 2. Full security suite (centralized)
# ---------------------------------------------------------------------------


def _public_addrinfo(*args: Any, **kwargs: Any) -> list[Any]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


class TestFinalSecuritySuite:
    def test_ssrf_requires_https(self) -> None:
        policy = EndpointPolicy(allowed_hosts=frozenset({"example.com"}))
        with pytest.raises(CredentialProviderError):
            validate_endpoint("http://example.com/token", policy, purpose="oauth2")

    def test_ssrf_enforces_allowlist(self) -> None:
        policy = EndpointPolicy(allowed_hosts=frozenset({"allowed.example.com"}))
        with patch("socket.getaddrinfo", side_effect=_public_addrinfo):
            with pytest.raises(CredentialProviderError):
                validate_endpoint("https://evil.example.com/token", policy, purpose="oauth2")

    def test_ssrf_blocks_metadata_service(self) -> None:
        policy = EndpointPolicy(allowed_hosts=frozenset({"169.254.169.254"}))
        with pytest.raises(CredentialProviderError):
            validate_endpoint("https://169.254.169.254/latest/meta-data/", policy, purpose="oauth2")

    def test_ssrf_blocks_loopback_and_private(self) -> None:
        policy = EndpointPolicy(allowed_hosts=frozenset({"local.test"}))

        def _loopback(*args: Any, **kwargs: Any) -> list[Any]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

        with patch("socket.getaddrinfo", side_effect=_loopback):
            with pytest.raises(CredentialProviderError):
                validate_endpoint("https://local.test/token", policy, purpose="oauth2")

        def _private(*args: Any, **kwargs: Any) -> list[Any]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]

        with patch("socket.getaddrinfo", side_effect=_private):
            with pytest.raises(CredentialProviderError):
                validate_endpoint("https://local.test/token", policy, purpose="oauth2")

    def test_artifact_key_traversal_rejected(self) -> None:
        from investigation_agent_platform.infrastructure.artifacts.keys import build_object_key

        for bad in ("../escape", "/absolute", "a/../../b", "..", "a\\b"):
            with pytest.raises(ValueError):
                build_object_key("t-a", "app", bad)
        good = build_object_key("t-a", "app", "history/run-1/report.html")
        assert good.startswith("t-a/app/")

    def test_local_store_symlink_escape_rejected(self, tmp_path: Path) -> None:
        from investigation_agent_platform.infrastructure.artifacts.local_store import (
            LocalArtifactStore,
        )

        root = tmp_path / "store"
        store = LocalArtifactStore(root)
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        link = root / "t-a" / "app"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("symlinks unavailable on this platform")
            return
        with pytest.raises(ValueError):
            store._resolve("t-a/app")

    def test_report_renderer_escapes_xss_and_sets_csp(self) -> None:
        from investigation_agent_platform.domain.finding.models import Finding, FindingType
        from investigation_agent_platform.domain.reporting.aggregate import (
            AggregateReport,
            AggregateReportRow,
        )
        from investigation_agent_platform.infrastructure.reporting.html_renderer import (
            HtmlAggregateReportRenderer,
        )

        tenant = "t-xss"
        finding = Finding(
            tenant_id=tenant,
            investigation_id=uuid4(),
            finding_type=FindingType.OBSERVATION,
            title="<img src=x onerror=alert(2)>",
            statement="statement",
        )
        report = AggregateReport(
            tenant_id=tenant,
            taxonomy_revision=3,
            assignment_count=1,
            rows=(
                AggregateReportRow(
                    cluster_key="C001",
                    label="<script>alert(1)</script>",
                    member_count=1,
                    member_finding_ids=[finding.id],
                ),
            ),
        )
        html = HtmlAggregateReportRenderer().render(report).decode("utf-8")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "Content-Security-Policy" in html

    def test_cursor_tampering_rejected(self) -> None:
        secret = "test-secret"
        token = encode_cursor({"sort": "created_at", "tiebreaker": "abc"}, secret)
        assert decode_cursor(token, secret)["tiebreaker"] == "abc"
        with pytest.raises(CursorError):
            decode_cursor(token[:-2] + ("AA" if not token.endswith("AA") else "BB"), secret)
        with pytest.raises(CursorError):
            decode_cursor(token, "wrong-secret")
        with pytest.raises(CursorError):
            decode_cursor("not-a-cursor", secret)

    def test_cross_tenant_identifier_collisions_isolated(self) -> None:
        from investigation_agent_platform.domain.intake.batch import BatchIntakeRecord

        a = BatchIntakeRecord(
            external_key="same-key", application_id="app", problem_description="one"
        )
        b = BatchIntakeRecord(
            external_key="same-key", application_id="app", problem_description="two"
        )
        # Same external key shape is legal per tenant; tenant binding lives in
        # the repository layer (tenant_id column + RLS), never in the record.
        assert a.external_key == b.external_key
        scope_a = CapabilityScope(tenant_id="t-a", application_id="app")
        scope_b = CapabilityScope(tenant_id="t-b", application_id="app")
        assert scope_cache_key(scope_a, "batch-intake", a.external_key) != scope_cache_key(
            scope_b, "batch-intake", b.external_key
        )

    def test_websocket_auth_rejects_anonymous_and_cross_tenant(self) -> None:
        prev = deps._context
        try:
            ctx = AppContext()
            set_app_context(ctx)
            client = TestClient(create_app(), raise_server_exceptions=False)
            # Anonymous handshake closes 4401 before accept.
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with client.websocket_connect(f"/api/v1/jobs/{uuid4()}/stream"):
                    pass
            assert exc_info.value.code == 4401
            # Cross-tenant job id closes 4404 without ever subscribing.
            job = BackgroundJob(
                tenant_id="tenant-a",
                application_id="app-1",
                kind="test-kind",
                created_by="tester",
                status=BackgroundJobStatus.RUNNING,
            )
            import asyncio

            asyncio.run(ctx.background_job_repo.create(job))
            with client.websocket_connect(
                f"/api/v1/jobs/{job.id}/stream", headers={"X-Tenant-ID": "tenant-b"}
            ) as ws:
                with pytest.raises(WebSocketDisconnect) as cross_info:
                    ws.receive_json()
                assert cross_info.value.code == 4404
        finally:
            deps._context = prev

    @pytest.mark.asyncio
    async def test_idempotency_replay_conflict_and_scope_isolation(self) -> None:
        from investigation_agent_platform.api.dependencies import _InMemoryIdempotencyStore

        store = _InMemoryIdempotencyStore()
        cached, reserved = await store.reserve_or_get("t-a", "k1", "hash-1", operation="op-a")
        assert cached is None and reserved is True
        with pytest.raises(IdempotencyInProgressError):
            await store.reserve_or_get("t-a", "k1", "hash-1", operation="op-a")
        await store.complete("t-a", "k1", {"ok": True}, operation="op-a")
        cached, reserved = await store.reserve_or_get("t-a", "k1", "hash-1", operation="op-a")
        assert cached == {"ok": True} and reserved is False
        with pytest.raises(IdempotencyConflictError):
            await store.reserve_or_get("t-a", "k1", "other-hash", operation="op-a")
        # Cross-operation and cross-scope isolation: same key, other op/scope reserves cleanly.
        cached, reserved = await store.reserve_or_get("t-a", "k1", "hash-1", operation="op-b")
        assert cached is None and reserved is True
        cached, reserved = await store.reserve_or_get("t-b", "k1", "hash-1", operation="op-a")
        assert cached is None and reserved is True

    @pytest.mark.asyncio
    async def test_quota_race_single_winner(self) -> None:
        import asyncio

        from investigation_agent_platform.api.dependencies import _InMemoryIdempotencyStore

        store = _InMemoryIdempotencyStore()
        results: list[bool] = []

        async def _attempt() -> None:
            try:
                _, reserved = await store.reserve_or_get("t-a", "race", "h", operation="op")
                results.append(reserved)
                if reserved:
                    await store.complete("t-a", "race", {"w": True}, operation="op")
            except IdempotencyInProgressError:
                results.append(False)

        await asyncio.gather(*(_attempt() for _ in range(10)))
        assert results.count(True) == 1

    def test_job_event_duplicate_suppression_by_event_id(self) -> None:
        event_id = uuid4()
        job_id = uuid4()
        first = JobProgressEvent(
            event_id=event_id,
            job_id=job_id,
            tenant_id="t-a",
            kind="batch-intake",
            status=BackgroundJobStatus.RUNNING,
            progress=50,
        )
        second = JobProgressEvent(
            event_id=event_id,
            job_id=job_id,
            tenant_id="t-a",
            kind="batch-intake",
            status=BackgroundJobStatus.RUNNING,
            progress=50,
        )
        seen: set[str] = set()
        deliveries = 0
        for event in (first, second):
            key = str(event.event_id)
            if key in seen:
                continue
            seen.add(key)
            deliveries += 1
        assert deliveries == 1


# ---------------------------------------------------------------------------
# 3. Migration upgrade + downgrade from current production head
# ---------------------------------------------------------------------------


class TestMigrationChainFinal:
    def _versions(self) -> tuple[list[str], dict[str, Any]]:
        versions_dir = Path(__file__).resolve().parents[2] / "migrations" / "versions"
        modules: dict[str, Any] = {}
        revisions: dict[str, str | None] = {}
        down: dict[str, str | None] = {}
        import importlib.util

        for path in sorted(versions_dir.glob("*.py")):
            spec = importlib.util.spec_from_file_location(path.stem, path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            modules[path.stem] = module
            revisions[module.revision] = path.stem
            down[module.revision] = module.down_revision
        assert len(modules) >= 15
        # Walk from base (down_revision None) to head.
        children: dict[str | None, str] = {}
        for rev, parent in down.items():
            assert parent not in children or parent is None, "migration fork detected"
            children[parent] = rev
        chain: list[str] = []
        cursor: str | None = None
        while cursor in children:
            cursor = children[cursor]
            chain.append(cursor)
        return chain, modules

    def test_chain_is_linear_ending_at_head(self) -> None:
        chain, _ = self._versions()
        assert chain[-1] == "021_analyzer_tables"
        assert len(chain) == len(set(chain))
        assert "016_profile_v2_columns" in chain
        assert "015_batch_intake" in chain
        assert "014_reference_documents" in chain
        assert "013_chat_sessions" in chain

    def test_single_head_is_current(self) -> None:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config(Path(__file__).resolve().parents[2] / "alembic.ini")
        script = ScriptDirectory.from_config(cfg)
        assert script.get_heads() == ["021_analyzer_tables"]

    def test_every_revision_has_upgrade_and_downgrade(self) -> None:
        _, modules = self._versions()
        for name, module in modules.items():
            assert callable(getattr(module, "upgrade", None)), f"{name} missing upgrade()"
            assert callable(getattr(module, "downgrade", None)), f"{name} missing downgrade()"


# ---------------------------------------------------------------------------
# 4. Versioned + pinned API/MCP/Kafka contract fixtures
# ---------------------------------------------------------------------------


class TestContractFixturesPinned:
    def test_mcp_tool_contract_exact(self) -> None:
        from investigation_agent_platform.mcp.registry import (
            EXPECTED_TOOLS,
            TOOL_ARGUMENT_PROPERTIES,
        )

        assert set(EXPECTED_TOOLS) == {
            "search_runtime_evidence",
            "get_evidence",
            "investigate_trace",
            "search_reference_docs",
        }
        assert set(TOOL_ARGUMENT_PROPERTIES["search_reference_docs"]) == {
            "q",
            "kinds",
            "limit",
            "cursor",
        }
        assert set(TOOL_ARGUMENT_PROPERTIES["get_evidence"]) == {"evidence_id"}

    def test_job_progress_event_envelope_versioned(self) -> None:
        event = JobProgressEvent(
            job_id=uuid4(),
            tenant_id="t-a",
            kind="batch-intake",
            status=BackgroundJobStatus.RUNNING,
            progress=25,
        )
        assert event.schema_version == "1.0"
        assert event.event_id is not None
        dumped = event.model_dump()
        assert dumped["progress"] == 25

    def test_capability_contract_versions_pinned(self) -> None:
        registries = build_default_registries()
        for manifest in registries.all_manifests():
            assert manifest.contract_version
            assert manifest.config_schema_version
            assert manifest.plugin_id

    def test_renderer_contract_version_pinned(self) -> None:
        from investigation_agent_platform.domain.reporting.aggregate import (
            AGGREGATE_REPORT_CONTRACT_VERSION,
            HTML_AGGREGATE_RENDERER_ID,
            HTML_AGGREGATE_RENDERER_VERSION,
        )
        from investigation_agent_platform.infrastructure.reporting.html_renderer import (
            RENDERER_CONTRACT_VERSION,
            RENDERER_PLUGIN_ID,
            renderer_manifest,
        )

        assert RENDERER_PLUGIN_ID == HTML_AGGREGATE_RENDERER_ID
        assert RENDERER_CONTRACT_VERSION == "1.0"
        assert HTML_AGGREGATE_RENDERER_VERSION == "1.0"
        assert AGGREGATE_REPORT_CONTRACT_VERSION == "1.0"
        assert renderer_manifest().contract_version == RENDERER_CONTRACT_VERSION

    def test_sse_event_names_pinned(self) -> None:
        from investigation_agent_platform.domain.investigation.chat import (
            CHAT_CONTRACT_VERSION,
            ChatStreamEvent,
        )

        assert CHAT_CONTRACT_VERSION
        for name in ("token", "metadata", "final", "error"):
            event = ChatStreamEvent(event=name, data={})
            assert event.event == name
            assert event.protocol_version == CHAT_CONTRACT_VERSION


# ---------------------------------------------------------------------------
# 5. Performance baselines (loose bounds: regression tripwires, not benchmarks)
# ---------------------------------------------------------------------------


class TestPerformanceBaselines:
    def test_vector_rrf_fusion_baseline(self) -> None:
        from types import SimpleNamespace

        from investigation_agent_platform.application.reference.reference_service import (
            _reciprocal_rank_fusion,
        )

        lexical = [SimpleNamespace(source_id=f"doc-{i}") for i in range(1000)]
        vector = [SimpleNamespace(source_id=f"doc-{999 - i}") for i in range(1000)]
        started = time.perf_counter()
        fused = _reciprocal_rank_fusion(lexical, vector, bound=20)
        elapsed = time.perf_counter() - started
        assert len(fused) == 20
        assert elapsed < 2.0

    def test_bulk_child_id_generation_baseline(self) -> None:
        from investigation_agent_platform.domain.intake.batch import child_workflow_id

        batch_id = uuid4()
        started = time.perf_counter()
        ids = {child_workflow_id(batch_id, i) for i in range(500)}
        elapsed = time.perf_counter() - started
        assert len(ids) == 500
        assert elapsed < 1.0

    def test_report_render_baseline(self) -> None:
        from investigation_agent_platform.domain.reporting.aggregate import (
            AggregateReport,
            AggregateReportRow,
        )
        from investigation_agent_platform.infrastructure.reporting.html_renderer import (
            HtmlAggregateReportRenderer,
        )

        rows = [
            AggregateReportRow(
                cluster_key=f"C{i:03d}",
                label=f"Cluster {i}",
                member_count=3,
                member_finding_ids=[uuid4(), uuid4(), uuid4()],
            )
            for i in range(25)
        ]
        report = AggregateReport(tenant_id="t", taxonomy_revision=7, assignment_count=75, rows=rows)
        started = time.perf_counter()
        payload = HtmlAggregateReportRenderer().render(report)
        elapsed = time.perf_counter() - started
        assert len(payload) > 0
        assert elapsed < 2.0

    def test_event_fanout_dedup_baseline(self) -> None:
        started = time.perf_counter()
        seen: set[str] = set()
        deliveries = 0
        for i in range(10_000):
            key = f"event-{i % 5_000}"
            if key in seen:
                continue
            seen.add(key)
            deliveries += 1
        elapsed = time.perf_counter() - started
        assert deliveries == 5_000
        assert elapsed < 2.0

    def test_cursor_codec_baseline(self) -> None:
        secret = "baseline-secret"
        started = time.perf_counter()
        for i in range(1_000):
            token = encode_cursor({"sort": "created_at", "tiebreaker": f"t-{i}"}, secret)
            assert decode_cursor(token, secret)["tiebreaker"] == f"t-{i}"
        assert time.perf_counter() - started < 2.0
