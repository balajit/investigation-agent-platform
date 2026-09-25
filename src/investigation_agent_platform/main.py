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

_cfg: ApplicationConfig | None = None
try:
    _cfg = load_application_config_from_env()
    from investigation_agent_platform.bootstrap import build_app_context

    build_app_context(_cfg)
    logger.info("Bootstrapped AppContext from environment", extra={"environment": _cfg.environment})
except PlatformConfigurationError as exc:
    logger.info(
        "Running with in-memory AppContext (config not available)", extra={"error": str(exc)}
    )
except Exception as exc:  # pragma: no cover - defensive
    logger.warning(
        "Failed to bootstrap AppContext, using in-memory fallback", extra={"error": str(exc)}
    )


def _create_lifespan_app() -> FastAPI:
    app = create_app()

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(api: FastAPI):  # type: ignore[no-untyped-def]
        # Graceful startup: AppContext already built via bootstrap above
        logger.info("Application startup complete")
        if original_lifespan is not None:
            async with original_lifespan(api):
                yield
        else:
            yield
        # Graceful shutdown: close engine/broker if present
        try:
            from investigation_agent_platform.api.dependencies import get_app_context

            ctx = get_app_context()
            engine = getattr(ctx, "engine", None)
            if engine is not None:
                await engine.dispose()
                logger.info("Disposed database engine on shutdown")
            broker = getattr(ctx, "broker", None)
            if broker is not None and hasattr(broker, "close"):
                maybe = broker.close()
                if asyncio.iscoroutine(maybe):
                    await maybe
        except Exception as exc:
            logger.warning("Error during shutdown cleanup: %s", exc)
        logger.info("Application shutdown complete")

    app.router.lifespan_context = lifespan  # type: ignore[attr-defined]
    return app


app = _create_lifespan_app()


def main() -> None:
    """Launches the FastAPI web gateway (``uvicorn investigation_agent_platform.main:app``)."""
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")


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
