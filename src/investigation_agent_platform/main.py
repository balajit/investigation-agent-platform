"""Application entrypoint: FastAPI gateway and Temporal worker launcher (Part 4, sections 3-4, 9.4)."""

from __future__ import annotations

import asyncio
import logging
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

# TO_DO-006: attach console + rotating-file JSON pipeline first so every
# subsequent startup record (including the layer report) lands in the file.
try:
    from investigation_agent_platform.infrastructure.telemetry.logging import (
        configure_logging,
    )

    configure_logging()
except Exception as exc:  # pragma: no cover - logging must never break boot
    logger.warning("Logging pipeline skipped", extra={"error": str(exc)})

# TO_DO-004 hierarchy: `.env*` files, then `config/application.yaml` (when
# present) fill unset names only; runtime env and the secret-manager chain
# always win. Runs before any config load in this module or the worker entry.
try:
    from investigation_agent_platform.infrastructure.configuration.layers import (
        apply_layered_config,
        effective_environment,
    )

    _layer_report = apply_layered_config()
    logger.info(
        "Config layers ready",
        extra={"dotenv": _layer_report.dotenv_files, "yaml": _layer_report.yaml_path},
    )
except Exception as exc:  # pragma: no cover - layering must never break boot
    logger.warning("Layered config skipped", extra={"error": str(exc)})

_cfg: ApplicationConfig | None = None
try:
    _cfg = load_application_config_from_env()
except Exception as exc:  # pragma: no cover - defensive
    logger.warning(
        "Failed to load application config; using development in-memory AppContext",
        extra={"error": str(exc)},
    )
    _cfg = None

# OTel: configure the GLOBAL tracer provider in every environment where
# telemetry is enabled. The observability adapter is only constructed on the
# production context path, but spans must export from dev processes too
# (collector on loopback :4317). Never breaks boot; no-op without the
# exporter package or with telemetry disabled.
try:
    import os as _os

    if (
        _os.environ.get("IAP_TELEMETRY_ENABLED", "true").lower() == "true"
        and "PYTEST_CURRENT_TEST" not in _os.environ
        and "pytest" not in sys.modules
    ):
        from investigation_agent_platform.infrastructure.observability.tracing import (
            configure_tracing,
        )

        _telemetry_cfg = getattr(_cfg, "telemetry", None)
        configure_tracing(
            service_name=getattr(_telemetry_cfg, "service_name", "investigation-agent-platform"),
            otlp_endpoint=(
                _os.environ.get("IAP_OTLP_ENDPOINT")
                or getattr(_telemetry_cfg, "otlp_endpoint", "http://localhost:4317")
            ),
            sample_ratio=float(
                _os.environ.get("IAP_OTEL_SAMPLE_RATIO")
                or getattr(_telemetry_cfg, "sample_ratio", 1.0)
            ),
        )
except Exception as exc:  # pragma: no cover - tracing must never break boot
    logger.warning("OTLP tracing setup skipped", extra={"error": str(exc)})

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


def _effective_env() -> str:
    """Environment for gating dev-only paths (TO_DO-005: single source).

    The loaded config wins; without one the process cannot be production.
    """
    try:
        return effective_environment(_cfg)
    except Exception:
        return "development"


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
        if _effective_env() == "production":
            return
        try:
            from investigation_agent_platform.api.dependencies import get_app_context

            ctx = get_app_context()
            if getattr(ctx, "temporal_client", None) is not None:
                return
            cfg = _cfg or load_application_config_from_env()
            from temporalio.client import Client as _TemporalClient

            # Retry with backoff: under host contention the handshake can time
            # out transiently while the server itself is healthy. Failing fast
            # here permanently disables dispatch (/start -> 503) until the
            # next restart, so keep trying for a few minutes before degrading.
            last_exc: Exception | None = None
            for attempt in range(8):
                try:
                    from investigation_agent_platform.infrastructure.observability.temporal_interceptor import (
                        temporal_interceptors as _temporal_interceptors,
                    )

                    ctx.temporal_client = await _TemporalClient.connect(  # type: ignore[attr-defined]
                        cfg.temporal.target_host,
                        namespace=cfg.temporal.namespace,
                        interceptors=_temporal_interceptors(),
                    )
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    await asyncio.sleep(min(30.0, 2.0 * (attempt + 1)))
            if last_exc is not None:
                raise last_exc
            ctx.temporal_config = cfg.temporal  # type: ignore[attr-defined]
            logger.info(
                "Wired API Temporal client",
                extra={"target": cfg.temporal.target_host},
            )
        except Exception as exc:
            logger.warning(
                "Temporal unavailable; workflow dispatch disabled in API",
                extra={"error": str(exc)},
                exc_info=True,
            )

    async def _maybe_wire_buffered() -> None:
        """Dev-only buffered persistence (Postgres write-through + WAL fallback).

        Wraps the lifespan's in-memory ``AppContext`` stores with buffered
        proxies so writes land in Postgres whenever reachable and journal
        locally otherwise, then starts the background flusher. Production
        never takes this path (F-001). Any failure leaves the plain context
        untouched — startup must never break on buffering.
        """
        if _effective_env() == "production":
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
            # Re-wire the evidence gateway AFTER proxy installation (same
            # reason as the worker path in bootstrap/worker.py): the
            # build-time gateway holds bare in-memory repo references.
            try:
                from investigation_agent_platform.bootstrap import _wire_evidence_gateway

                _wire_evidence_gateway(ctx, cfg)
            except Exception as exc2:
                logger.warning(
                    "API evidence gateway re-wire unavailable",
                    extra={"error": str(exc2)},
                    exc_info=True,
                )
            start_buffer_flusher(get_app_context())
        except Exception as exc:
            from investigation_agent_platform.domain.common.exceptions import (
                SplitBrainStoreError,
            )

            if isinstance(exc, SplitBrainStoreError):
                # Split-brain boot (amnesiac store with a reachable shared
                # Postgres) must fail loud, never serve. Other config errors
                # (e.g. missing env vars) keep the pre-existing
                # warn-and-continue path.
                raise
            logger.warning(
                "Buffered persistence unavailable; continuing in-memory",
                extra={"error": str(exc)},
                exc_info=True,
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
            logger.warning("Job messaging unavailable", extra={"error": str(exc)}, exc_info=True)

    async def _maybe_seed_dev() -> None:
        """Opt-in UI dev dataset (IAP_DEV_SEED_TENANTS, e.g. "n-a").

        Runs AFTER _maybe_wire_buffered so rows go through the write-through
        proxies into Postgres: API reads (primary-first) and the Temporal
        worker (separate process) both see them. Seeding before proxy
        installation leaves rows in raw memory, shadowed and worker-invisible.
        Idempotent per tenant (skips when the profile exists). Never raises:
        a seed failure must not break startup.
        """
        if _effective_env() == "production":
            return
        try:
            from investigation_agent_platform.api.dependencies import get_app_context
            from investigation_agent_platform.api.dev_seed import (
                seed_dev_tenants,
                seed_tenants_from_env,
            )

            if not seed_tenants_from_env():
                return
            result = await seed_dev_tenants(get_app_context())
            logger.info("Dev seed complete", extra={"seeded": result})
        except Exception as exc:
            logger.warning(
                "Dev seed failed; continuing unseeded", extra={"error": str(exc)}, exc_info=True
            )

    @asynccontextmanager
    async def lifespan(api: FastAPI):  # type: ignore[no-untyped-def]
        # Graceful startup: AppContext already built via bootstrap above
        logger.info("Application startup complete")
        if original_lifespan is not None:
            async with original_lifespan(api):
                await _maybe_wire_temporal()
                await _maybe_wire_buffered()
                await _maybe_seed_dev()
                await _maybe_start_job_messaging()
                yield
        else:
            await _maybe_wire_temporal()
            await _maybe_wire_buffered()
            await _maybe_seed_dev()
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
    import os

    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")  # nosec B104 - containerized deployment binds all interfaces by design; network policy enforced outside the process


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
