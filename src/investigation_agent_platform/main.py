"""Application entrypoint: FastAPI gateway and Temporal worker launcher (Part 4, sections 3-4, 9.4)."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError
from investigation_agent_platform.infrastructure.configuration.config import (
    ApplicationConfig,
    load_application_config_from_env,
)

logger = logging.getLogger(__name__)

_cfg: ApplicationConfig | None = None
try:
    _cfg = load_application_config_from_env()
except Exception as exc:  # pragma: no cover - defensive
    logger.warning(
        "Failed to load application config; using development in-memory AppContext",
        extra={"error": str(exc)},
    )
    _cfg = None

if _cfg is not None:
    from investigation_agent_platform.bootstrap import build_app_context

    if (_cfg.environment or "").lower() == "production":
        # Production must fail hard on wiring errors (F-001/F-002): never
        # silently degrade to in-memory/permissive dependencies.
        build_app_context(_cfg)
        logger.info("Bootstrapped production AppContext", extra={"environment": _cfg.environment})
    else:
        try:
            build_app_context(_cfg)
            logger.info(
                "Bootstrapped AppContext from environment", extra={"environment": _cfg.environment}
            )
        except PlatformConfigurationError as exc:
            logger.info(
                "Running with in-memory AppContext (non-production, config incomplete)",
                extra={"error": str(exc)},
            )


def _create_lifespan_app() -> FastAPI:
    from investigation_agent_platform.api.dependencies import Container, get_app_context

    # Part 11.0: pass the already-bootstrapped AppContext explicitly so the
    # lifespan preserves production wiring instead of replacing it. When no
    # config loaded (_cfg is None), get_app_context() yields the default
    # in-memory context — identical to previous dev behavior, but explicit.
    app = create_app(container=Container(context=get_app_context()))

    original_lifespan = app.router.lifespan_context

    async def _maybe_wire_temporal() -> None:
        """Best-effort Temporal client for the non-production API process.

        The factory lifespan installs a bare in-memory ``AppContext`` (wiping
        the bootstrap context), so without this the dev API can never dispatch
        workflows (``/start`` → 503) even with Temporal running next to it.
        Production wires its client mandatorily in bootstrap; here a failure
        just leaves dispatch disabled (routers report 503, never silent 200).
        """
        if os.environ.get("IAP_ENVIRONMENT", "development").lower() == "production":
            return
        try:
            from investigation_agent_platform.api.dependencies import get_app_context

            ctx = get_app_context()
            if getattr(ctx, "temporal_client", None) is not None:
                return
            cfg = _cfg or load_application_config_from_env()
            from temporalio.client import Client as _TemporalClient

            ctx.temporal_client = await _TemporalClient.connect(  # type: ignore[attr-defined]
                cfg.temporal.target_host, namespace=cfg.temporal.namespace
            )
            ctx.temporal_config = cfg.temporal  # type: ignore[attr-defined]
            logger.info(
                "Wired API Temporal client",
                extra={"target": cfg.temporal.target_host},
            )
        except Exception as exc:
            logger.warning(
                "Temporal unavailable; workflow dispatch disabled in API",
                extra={"error": str(exc)},
            )

    async def _maybe_wire_buffered() -> None:
        """Dev-only buffered persistence (Postgres write-through + WAL fallback).

        Wraps the lifespan's in-memory ``AppContext`` stores with buffered
        proxies so writes land in Postgres whenever reachable and journal
        locally otherwise, then starts the background flusher. Production
        never takes this path (F-001). Any failure leaves the plain context
        untouched — startup must never break on buffering.
        """
        if os.environ.get("IAP_ENVIRONMENT", "development").lower() == "production":
            return
        try:
            from investigation_agent_platform.api.dependencies import get_app_context
            from investigation_agent_platform.bootstrap.buffered_dev import (
                ensure_buffered_dev_context,
                start_buffer_flusher,
            )

            cfg = _cfg or load_application_config_from_env()
            ctx = get_app_context()
            await ensure_buffered_dev_context(ctx, cfg)
            start_buffer_flusher(get_app_context())
        except Exception as exc:
            logger.warning(
                "Buffered persistence unavailable; continuing in-memory",
                extra={"error": str(exc)},
            )

    async def _maybe_start_job_messaging() -> None:
        """Start the per-replica job progress subscriber (Part 11.3C/11.12).

        One pattern subscriber with a replica-unique group; missing broker
        (dev) skips quietly, Kafka outages degrade to DB snapshots + local
        hub — progress streaming is supplemental, never boot-critical.
        """
        try:
            from investigation_agent_platform.api.dependencies import get_app_context
            from investigation_agent_platform.infrastructure.messaging.job_fanout import (
                ensure_job_messaging,
            )

            await ensure_job_messaging(get_app_context())
        except Exception as exc:
            logger.warning("Job messaging unavailable", extra={"error": str(exc)})

    @asynccontextmanager
    async def lifespan(api: FastAPI):  # type: ignore[no-untyped-def]
        # Graceful startup: AppContext already built via bootstrap above
        logger.info("Application startup complete")
        if original_lifespan is not None:
            async with original_lifespan(api):
                await _maybe_wire_temporal()
                await _maybe_wire_buffered()
                await _maybe_start_job_messaging()
                yield
        else:
            await _maybe_wire_temporal()
            await _maybe_wire_buffered()
            await _maybe_start_job_messaging()
            yield
        # Graceful shutdown: the inner lifespan already ran the ordered
        # shutdown_app_context(); this second call is an idempotent no-op
        # that covers paths where the inner lifespan was bypassed.
        try:
            from investigation_agent_platform.api.dependencies import (
                get_app_context,
                shutdown_app_context,
            )

            await shutdown_app_context(get_app_context())
        except Exception as exc:
            logger.warning("Error during shutdown cleanup: %s", exc)
        logger.info("Application shutdown complete")

    app.router.lifespan_context = lifespan  # type: ignore[attr-defined]
    return app


app = _create_lifespan_app()


def main() -> None:
    """Launches the FastAPI web gateway (``uvicorn investigation_agent_platform.main:app``)."""
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")  # nosec B104 - containerized deployment binds all interfaces by design; network policy enforced outside the process


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        from investigation_agent_platform.bootstrap.worker import run_temporal_worker

        _worker_cfg = _cfg or load_application_config_from_env()

        # Handle graceful shutdown signals
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        def _handle_signal(signum: int, _frame: object) -> None:
            logger.info("Received signal %s, shutting down worker", signum)
            for task in asyncio.all_tasks(loop):
                task.cancel()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handle_signal)  # type: ignore[arg-type]
            except ValueError:
                pass  # not in main thread

        loop.run_until_complete(run_temporal_worker(_worker_cfg))
    else:
        main()
