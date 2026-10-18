# tests/unit/test_part11_phase0.py
"""Part 11 Phase 11.0 tests: platform correctness prerequisites.

Covers: lifespan preservation of the bootstrapped AppContext, fail-closed
set_app_context, API/worker composition parity, canonical worker
registration, ordered idempotent shutdown, startup diagnostics secrecy, and
startup-failure behavior. No live infrastructure: all external clients are
fakes or mocks.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import (
    AppContext,
    Container,
    get_app_context,
    log_composition_summary,
    set_app_context,
    shutdown_app_context,
)


@pytest.fixture
def _restore_context():
    import investigation_agent_platform.api.dependencies as deps

    prev = deps._context
    yield
    deps._context = prev


def _marked_context() -> AppContext:
    """AppContext with sentinel production-style attachments."""
    ctx = AppContext()
    ctx.engine = MagicMock(name="engine")
    ctx.engine.dispose = AsyncMock(name="engine.dispose")
    ctx.temporal_client = MagicMock(name="temporal_client")
    ctx.broker = MagicMock(name="broker")
    ctx.evidence_gateway = MagicMock(name="evidence_gateway")
    return ctx


# ===========================================================================
# set_app_context fail-closed
# ===========================================================================


class TestSetAppContextStrict:
    def test_rejects_container(self, _restore_context: None) -> None:
        with pytest.raises(TypeError):
            set_app_context(Container())  # type: ignore[arg-type]

    def test_rejects_non_context(self, _restore_context: None) -> None:
        with pytest.raises(TypeError):
            set_app_context("not-a-context")  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            set_app_context(None)  # type: ignore[arg-type]

    def test_accepts_app_context_identity(self, _restore_context: None) -> None:
        ctx = AppContext()
        set_app_context(ctx)
        assert get_app_context() is ctx


# ===========================================================================
# Container explicit composition
# ===========================================================================


class TestContainer:
    @pytest.mark.asyncio
    async def test_explicit_context_preserved(self) -> None:
        ctx = AppContext()
        container = Container(context=ctx)
        await container.initialize()
        assert container.context is ctx
        # initialize() is idempotent
        await container.initialize()
        assert container.context is ctx

    @pytest.mark.asyncio
    async def test_default_builds_in_memory(self) -> None:
        container = Container()
        await container.initialize()
        assert isinstance(container.context, AppContext)
        assert container.context.engine is None
        assert container.context.temporal_client is None

    @pytest.mark.asyncio
    async def test_app_config_uses_shared_builder(self) -> None:
        sentinel = AppContext()
        cfg = MagicMock(name="app_config")
        with patch(
            "investigation_agent_platform.bootstrap.build_app_context",
            return_value=sentinel,
        ) as mock_build:
            container = Container(app_config=cfg)
            await container.initialize()
            mock_build.assert_called_once_with(cfg)
            assert container.context is sentinel

    @pytest.mark.asyncio
    async def test_builder_failure_propagates_fail_closed(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import (
            PlatformConfigurationError,
        )

        cfg = MagicMock(name="app_config")
        with patch(
            "investigation_agent_platform.bootstrap.build_app_context",
            side_effect=PlatformConfigurationError("no db"),
        ):
            container = Container(app_config=cfg)
            with pytest.raises(PlatformConfigurationError):
                await container.initialize()


# ===========================================================================
# Lifespan preservation (Part 11.0 core regression test)
# ===========================================================================


class TestLifespanPreservation:
    def test_lifespan_keeps_bootstrapped_adapters(self, _restore_context: None) -> None:
        """Production adapters installed before startup survive lifespan."""
        ctx = _marked_context()
        app = create_app(container=Container(context=ctx))
        with TestClient(app):
            live = get_app_context()
            assert live is ctx
            assert live.engine is ctx.engine
            assert live.temporal_client is not None
            assert live.evidence_gateway is not None
            assert live.broker is not None

    def test_lifespan_default_container_is_in_memory(self, _restore_context: None) -> None:
        app = create_app()
        with TestClient(app):
            live = get_app_context()
            assert isinstance(live, AppContext)
            assert live.engine is None

    def test_main_wires_current_context_into_container(
        self, _restore_context: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main._create_lifespan_app must pass the bootstrapped context through."""
        import investigation_agent_platform.main as main_mod

        ctx = _marked_context()
        set_app_context(ctx)
        captured: dict = {}

        def _fake_create_app(**kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return create_app(**kwargs)

        monkeypatch.setattr(main_mod, "create_app", _fake_create_app)
        main_mod._create_lifespan_app()
        assert captured["container"].context is ctx


# ===========================================================================
# API/worker composition parity
# ===========================================================================


class TestCompositionParity:
    def test_worker_delegates_to_shared_builder(self) -> None:
        from investigation_agent_platform.bootstrap import worker as worker_mod

        sentinel = AppContext()
        cfg = MagicMock(name="app_config")
        with patch.object(worker_mod, "_build_app_context", return_value=sentinel) as mock:
            assert worker_mod.build_app_context(cfg) is sentinel
            mock.assert_called_once_with(cfg)

    def test_canonical_registration_lists(self) -> None:
        from investigation_agent_platform.application.worker.workflows import (
            RunInvestigationWorkflow,
        )
        from investigation_agent_platform.bootstrap.worker import ACTIVITIES, WORKFLOWS

        assert RunInvestigationWorkflow in WORKFLOWS
        assert len(WORKFLOWS) >= 3
        assert len(ACTIVITIES) >= 9

    def test_legacy_run_worker_deprecated_and_delegated(self) -> None:
        import investigation_agent_platform.application.worker as legacy
        from investigation_agent_platform.bootstrap.worker import ACTIVITIES, WORKFLOWS

        with (
            patch.object(legacy, "Client") as mock_client,
            patch.object(legacy, "Worker") as mock_worker,
        ):
            mock_client.connect = AsyncMock(return_value=MagicMock())
            instance = MagicMock()
            instance.run = AsyncMock(return_value=None)
            mock_worker.return_value = instance
            import asyncio

            with pytest.warns(DeprecationWarning):
                asyncio.run(legacy.run_worker())
            _, kwargs = mock_worker.call_args
            assert list(kwargs["workflows"]) == list(WORKFLOWS)
            assert list(kwargs["activities"]) == list(ACTIVITIES)


# ===========================================================================
# Ordered idempotent shutdown
# ===========================================================================


class TestShutdown:
    @pytest.mark.asyncio
    async def test_bare_context_shutdown_noop(self) -> None:
        await shutdown_app_context(AppContext())

    @pytest.mark.asyncio
    async def test_shutdown_closes_once_in_order(self) -> None:
        order: list[str] = []
        ctx = AppContext()

        async def _flusher(_ctx: AppContext) -> None:
            order.append("flusher")

        from investigation_agent_platform.bootstrap import buffered_dev

        broker = MagicMock(name="broker")
        broker.close = MagicMock(name="broker.close", return_value=None)
        engine = MagicMock(name="engine")
        engine.dispose = AsyncMock(name="engine.dispose")
        ctx.broker = broker
        ctx.engine = engine

        with patch.object(buffered_dev, "stop_buffer_flusher", side_effect=_flusher) as mock_stop:
            await shutdown_app_context(ctx)
            await shutdown_app_context(ctx)
            mock_stop.assert_called_once_with(ctx)
        assert order == ["flusher"]
        broker.close.assert_called_once_with()
        engine.dispose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_tolerates_failing_clients(self) -> None:
        ctx = AppContext()
        broker = MagicMock(name="broker")
        broker.close.side_effect = RuntimeError("broker down")
        engine = MagicMock(name="engine")
        engine.dispose = AsyncMock(side_effect=RuntimeError("db down"))
        ctx.broker = broker
        ctx.engine = engine
        await shutdown_app_context(ctx)  # must not raise
        assert ctx._shutdown_done is True


# ===========================================================================
# Startup diagnostics secrecy
# ===========================================================================


class TestCompositionSummary:
    def test_names_implementations_without_secrets(
        self, _restore_context: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        ctx = _marked_context()
        with caplog.at_level(logging.INFO, logger="investigation_agent_platform.api.dependencies"):
            log_composition_summary(ctx)
        # stdlib formatting renders only the message; implementation names
        # travel in the record extras — assert on those, plus secrecy of both.
        assert caplog.records, "expected a composition summary log record"
        record = caplog.records[-1]
        assert record.__dict__.get("investigation_repo") == "InMemoryInvestigationRepository"
        assert record.__dict__.get("temporal") == "present"
        assert record.__dict__.get("broker") == "present"
        blob = caplog.text + repr(record.__dict__)
        for secret in ("localhost", "5432", "sk-", "5432/iap", "Bearer", "postgresql://"):
            assert secret not in blob
