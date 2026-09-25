"""FastAPI application factory wiring the v1 routing matrix, security, middleware, and lifecycle bounds (Part 4, section 4.1)."""

import logging
import time
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

import structlog
import structlog.contextvars
from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

# Ensure structlog is configured with contextvars merging if not already configured.
try:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
except Exception:
    pass

from investigation_agent_platform.api.dependencies import (
    ApiSettings,
    Container,
    set_app_context,
)
from investigation_agent_platform.api.v1.routers.events import router as events_router
from investigation_agent_platform.api.v1.routers.evidence import router as evidence_router
from investigation_agent_platform.api.v1.routers.health import router as health_router
from investigation_agent_platform.api.v1.routers.hypotheses import router as hypotheses_router
from investigation_agent_platform.api.v1.routers.investigations import (
    router as investigations_router,
)
from investigation_agent_platform.api.v1.routers.profiles import router as profiles_router
from investigation_agent_platform.api.v1.routers.timeline import router as timeline_router
from investigation_agent_platform.domain.common.exceptions import (
    ConcurrencyError,
    ConflictError,
    DomainException,
    EntityNotFoundError,
    UnauthorizedError,
    ValidationError,
)

logger = logging.getLogger(__name__)


def create_app(
    settings: ApiSettings | None = None,
    container: Container | None = None,
) -> FastAPI:
    """Builds and wires the FastAPI application composition root (Part 4, section 4.1)."""
    app_settings = settings or ApiSettings()
    app_container = container or Container(settings=app_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        logger.info(
            "Initializing application dependency container...",
            extra={"event": "startup", "environment": app_settings.environment},
        )
        await app_container.initialize()
        set_app_context(app_container)
        yield
        logger.info("Shutting down application dependency container...", extra={"event": "shutdown"})
        await app_container.shutdown()

    docs_url = "/docs" if app_settings.enable_docs or app_settings.environment != "production" else None
    openapi_url = "/openapi.json" if app_settings.enable_docs or app_settings.environment != "production" else None

    app = FastAPI(
        title="Investigation Agent Platform API",
        version="0.1.0",
        docs_url=docs_url,
        openapi_url=openapi_url,
        lifespan=lifespan,
    )

    app.state.settings = app_settings
    app.state.container = app_container

    @app.middleware("http")
    async def correlation_and_tracing_middleware(request: Request, call_next: Callable[..., Any]) -> Response:
        start_time = time.perf_counter()
        correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        request.state.correlation_id = correlation_id

        tenant_id = request.headers.get("X-Tenant-ID") or "anonymous"
        request.state.tenant_id = tenant_id

        structlog.contextvars.bind_contextvars(correlation_id=correlation_id, tenant_id=tenant_id)
        try:
            response: Response = await call_next(request)
            duration_ms = (time.perf_counter() - start_time) * 1000.0

            response.headers["X-Correlation-ID"] = correlation_id
            logger.info(
                f"{request.method} {request.url.path} HTTP/{request.scope.get('http_version', '1.1')} {response.status_code}",
                extra={
                    "correlation_id": correlation_id,
                    "tenant_id": tenant_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round(duration_ms, 2),
                },
            )
            return response
        finally:
            structlog.contextvars.clear_contextvars()

    @app.exception_handler(DomainException)
    async def handle_domain_exception(request: Request, exc: DomainException) -> JSONResponse:
        correlation_id = getattr(request.state, "correlation_id", "unknown")

        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        if isinstance(exc, ValidationError):
            status_code = status.HTTP_400_BAD_REQUEST
        elif isinstance(exc, UnauthorizedError):
            status_code = status.HTTP_403_FORBIDDEN
        elif isinstance(exc, EntityNotFoundError):
            status_code = status.HTTP_404_NOT_FOUND
        elif isinstance(exc, (ConcurrencyError, ConflictError)):
            status_code = status.HTTP_409_CONFLICT

        logger.warning(
            f"Domain exception mapped to HTTP {status_code}: {exc}",
            extra={
                "correlation_id": correlation_id,
                "exception_type": exc.__class__.__name__,
                "error_message": exc.message,
                "status_code": status_code,
            },
        )
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "code": exc.__class__.__name__,
                    "message": exc.message,
                    "correlation_id": correlation_id,
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_exception(request: Request, exc: RequestValidationError) -> JSONResponse:
        correlation_id = getattr(request.state, "correlation_id", "unknown")
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "ValidationError",
                    "message": "Invalid request parameters or payload",
                    "details": exc.errors(),
                    "correlation_id": correlation_id,
                }
            },
        )

    api_v1 = "/api/v1"
    app.include_router(health_router, prefix=api_v1)
    app.include_router(investigations_router, prefix=api_v1)
    app.include_router(evidence_router, prefix=api_v1)
    app.include_router(hypotheses_router, prefix=api_v1)
    app.include_router(timeline_router, prefix=api_v1)
    app.include_router(profiles_router, prefix=api_v1)
    app.include_router(events_router, prefix=api_v1)

    return app