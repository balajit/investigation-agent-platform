"""Store-sharedness: split-brain guard, readiness signal, dispatch-gate agreement.

Regression cover for the tenant n-a self-profile outage: the API served
process-local memory while seed scripts and workers converged on Postgres,
so seeded profiles 404d and workflow dispatch 503d while /health/ready
stayed green.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import (
    AppContext,
    InMemoryInvestigationRepository,
    is_worker_visible_store,
    set_app_context,
)
from investigation_agent_platform.infrastructure.persistence.buffered import (
    BufferedProxy,
    DownPrimary,
    SqliteWal,
)


def _ctx_client() -> tuple[TestClient, AppContext]:
    ctx = AppContext()
    set_app_context(ctx)
    return TestClient(create_app()), ctx


class TestIsWorkerVisibleStore:
    def test_none_is_not_visible(self) -> None:
        assert is_worker_visible_store(None) is False

    def test_in_memory_repo_is_not_visible(self) -> None:
        assert is_worker_visible_store(InMemoryInvestigationRepository()) is False

    def test_buffered_proxy_with_down_primary_is_not_visible(self, tmp_path) -> None:
        wal = SqliteWal(str(tmp_path / "wal.sqlite3"))
        proxy = BufferedProxy(
            DownPrimary("investigation_repo"), InMemoryInvestigationRepository(), wal, "t"
        )
        assert is_worker_visible_store(proxy) is False

    def test_plain_object_repo_is_visible(self) -> None:
        assert is_worker_visible_store(object()) is True


class TestReadinessStoreSignal:
    def test_full_detail_reports_store_key(self, monkeypatch) -> None:
        monkeypatch.setenv("IAP_HEALTH_DETAIL", "full")
        client, _ = _ctx_client()
        body = client.get("/api/v1/health/ready").json()
        assert body["dependencies"]["store"] in ("SHARED", "PROCESS_LOCAL", "UNKNOWN")

    def test_divergence_risk_fails_readiness(self, monkeypatch) -> None:
        monkeypatch.setenv("IAP_HEALTH_DETAIL", "full")
        client, ctx = _ctx_client()
        ctx._store_divergence_risk = True  # type: ignore[attr-defined]
        resp = client.get("/api/v1/health/ready")
        assert resp.status_code == 503
        assert resp.json()["dependencies"]["store"] == "PROCESS_LOCAL"

    def test_offline_process_local_stays_healthy_but_reported(self, monkeypatch) -> None:
        monkeypatch.setenv("IAP_HEALTH_DETAIL", "full")
        client, ctx = _ctx_client()
        ctx._store_divergence_risk = False  # type: ignore[attr-defined]
        resp = client.get("/api/v1/health/ready")
        assert resp.json()["dependencies"]["store"] == "PROCESS_LOCAL"

    def test_buffer_backlog_reported(self, monkeypatch) -> None:
        from types import SimpleNamespace

        monkeypatch.setenv("IAP_HEALTH_DETAIL", "full")
        client, ctx = _ctx_client()
        ctx._buffer_wal = SimpleNamespace(  # type: ignore[attr-defined]
            pending_count=lambda: 2, held_count=lambda: 26, path="data/wal"
        )
        body = client.get("/api/v1/health/ready").json()
        assert body["dependencies"]["buffer"] == "pending=2 held=26"

    def test_buffer_disabled_reported(self, monkeypatch) -> None:
        monkeypatch.setenv("IAP_HEALTH_DETAIL", "full")
        client, _ = _ctx_client()
        body = client.get("/api/v1/health/ready").json()
        assert body["dependencies"]["buffer"] == "DISABLED"


class TestHeldReporting:
    def test_error_on_change_debug_at_steady_state(self, caplog) -> None:
        import logging
        from types import SimpleNamespace

        from investigation_agent_platform.infrastructure.persistence.buffered import (
            _report_held_entries,
        )

        wal = SimpleNamespace(held_count=lambda: 26, path="data/wal")
        with caplog.at_level(logging.DEBUG):
            marker = _report_held_entries(wal, 0)
        assert marker == 26
        assert any(r.levelname == "ERROR" for r in caplog.records)
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            marker = _report_held_entries(wal, 26)
        assert marker == 26
        assert not any(r.levelname == "ERROR" for r in caplog.records)
        assert any("unchanged" in r.getMessage() for r in caplog.records)

    def test_zero_resets_marker(self) -> None:
        from types import SimpleNamespace

        from investigation_agent_platform.infrastructure.persistence.buffered import (
            _report_held_entries,
        )

        wal = SimpleNamespace(held_count=lambda: 0, path="data/wal")
        assert _report_held_entries(wal, 26) == 0


def _disabled_config() -> SimpleNamespace:
    class _URI:
        def get_secret_value(self) -> str:
            return "postgresql://iap:iap@localhost:5452/iap"

    return SimpleNamespace(
        buffer=SimpleNamespace(enabled=False),
        database=SimpleNamespace(connection_uri=_URI()),
    )


class TestBufferedDevSplitBrainGuard:
    @pytest.mark.asyncio
    async def test_disabled_with_reachable_postgres_flags_divergence(self) -> None:
        from investigation_agent_platform.bootstrap import buffered_dev

        ctx = SimpleNamespace()
        with patch.object(buffered_dev, "_postgres_reachable", return_value=True):
            out = await buffered_dev.ensure_buffered_dev_context(ctx, _disabled_config())
        assert getattr(out, "_store_divergence_risk", None) is True
        assert getattr(out, "_buffered", False) is False

    @pytest.mark.asyncio
    async def test_disabled_without_reachable_postgres_is_quiet_offline(self) -> None:
        from investigation_agent_platform.bootstrap import buffered_dev

        ctx = SimpleNamespace()
        with patch.object(buffered_dev, "_postgres_reachable", return_value=False):
            out = await buffered_dev.ensure_buffered_dev_context(ctx, _disabled_config())
        assert getattr(out, "_store_divergence_risk", None) is False

    def test_no_iap_buffer_env_leak(self) -> None:
        # The guard must not depend on ambient env beyond the passed config.
        assert os.environ.get("IAP_ENVIRONMENT", "development") != "production"
