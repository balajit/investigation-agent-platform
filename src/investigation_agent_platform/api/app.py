"""FastAPI application factory wiring the v1 routing matrix, security, middleware, and lifecycle bounds (Part 4, section 4.1)."""

import logging
import os
import re
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
    logging.getLogger(__name__).warning(
        "structlog.configure failed; falling back to stdlib logging only", exc_info=True
    )

from investigation_agent_platform.api.dependencies import (
    ApiSettings,
    Container,
    set_app_context,
)
from investigation_agent_platform.api.errors import public_error, sanitize_extra
from investigation_agent_platform.api.rate_limit import check_rate_limit
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

# F-016: correlation IDs are caller-controlled input — bound length and
# restricted to a safe character set to prevent log injection / oversized
# identifiers. Anything else is replaced with a freshly generated ID.
_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9_.\-:]{1,128}$")


def _sanitize_correlation_id(raw: str | None) -> str:
    if raw and _CORRELATION_ID_RE.match(raw):
        return raw
    return str(uuid.uuid4())


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
        logger.info(
            "Shutting down application dependency container...", extra={"event": "shutdown"}
        )
        await app_container.shutdown()

    docs_url = (
        "/docs" if app_settings.enable_docs or app_settings.environment != "production" else None
    )
    openapi_url = (
        "/openapi.json"
        if app_settings.enable_docs or app_settings.environment != "production"
        else None
    )

    # F-065: production docs are explicitly opt-in via IAP_ENABLE_DOCS=true.
    # A default-true settings flag must never expose docs in production.
    if (app_settings.environment or "").lower() == "production":
        docs_opt_in = (
            app_settings.enable_docs
            and os.environ.get("IAP_ENABLE_DOCS", "false").lower() == "true"
        )
        docs_url = "/docs" if docs_opt_in else None
        openapi_url = "/openapi.json" if docs_opt_in else None

    app = FastAPI(
        title="Investigation Agent Platform API",
        version="0.1.0",
        docs_url=docs_url,
        openapi_url=openapi_url,
        lifespan=lifespan,
    )

    # F-064: transport security policy — trusted hosts, CORS allow-list,
    # security headers. Forwarded headers are NOT trusted (no ProxyHeaders
    # middleware) unless the deployment explicitly terminates TLS at a
    # configured proxy in front of this app.
    trusted_hosts = [h.strip() for h in (app_settings.trusted_hosts or "").split(",") if h.strip()]
    if trusted_hosts:
        from fastapi.middleware.trustedhost import TrustedHostMiddleware

        app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

    cors_origins = [
        o.strip() for o in (app_settings.cors_allow_origins or "").split(",") if o.strip()
    ]
    if cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "X-Tenant-ID",
                "X-Idempotency-Key",
                "X-Correlation-ID",
            ],
            max_age=600,
        )

    @app.middleware("http")
    async def security_headers_middleware(
        request: Request, call_next: Callable[..., Any]
    ) -> Response:
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if (app_settings.environment or "").lower() == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    # F-060: tenant/principal-aware rate limiting. Disabled by default in
    # non-production (IAP_RATE_LIMIT_ENABLED=true to exercise); production
    # deployments should additionally inject a shared Redis store.
    from investigation_agent_platform.api.rate_limit import InMemoryRateLimitStore

    _rate_limit_store = InMemoryRateLimitStore()
    _rate_limit_enabled = (
        os.environ.get(
            "IAP_RATE_LIMIT_ENABLED",
            "true" if (app_settings.environment or "").lower() == "production" else "false",
        ).lower()
        == "true"
    )

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next: Callable[..., Any]) -> Response:
        if _rate_limit_enabled and request.url.path.startswith("/api/"):
            # Identity is best-effort here (verified later by dependencies);
            # the verified layer re-checks, so header spoofing gains nothing.
            tenant = request.headers.get("X-Tenant-ID")
            allowed, retry_after, klass = await check_rate_limit(
                _rate_limit_store, tenant, None, request.url.path
            )
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    content={
                        "error": {
                            "code": "RATE_LIMITED",
                            "message": f"Rate limit exceeded for {klass}; retry later.",
                            "correlation_id": getattr(request.state, "correlation_id", "unknown"),
                        }
                    },
                    headers={"Retry-After": str(retry_after)},
                )
        return await call_next(request)  # type: ignore[no-any-return]

    app.state.settings = app_settings
    app.state.container = app_container

    @app.middleware("http")
    async def correlation_and_tracing_middleware(
        request: Request, call_next: Callable[..., Any]
    ) -> Response:
        start_time = time.perf_counter()
        correlation_id = _sanitize_correlation_id(request.headers.get("X-Correlation-ID"))
        request.state.correlation_id = correlation_id

        # F-017: the caller-supplied tenant header is untrusted input and must
        # never be treated as an authenticated identity. It is recorded only
        # as `requested_tenant_id` for logging/diagnostics; the authoritative
        # `authenticated_tenant_id` is set later by the `require_tenant`
        # dependency once the request has actually been verified.
        requested_tenant_id = request.headers.get("X-Tenant-ID") or "anonymous"
        request.state.requested_tenant_id = requested_tenant_id
        request.state.authenticated_tenant_id = None

        structlog.contextvars.bind_contextvars(
            correlation_id=correlation_id, requested_tenant_id=requested_tenant_id
        )
        try:
            response: Response = await call_next(request)
            duration_ms = (time.perf_counter() - start_time) * 1000.0

            response.headers["X-Correlation-ID"] = correlation_id
            logger.info(
                f"{request.method} {request.url.path} HTTP/{request.scope.get('http_version', '1.1')} {response.status_code}",
                extra=sanitize_extra(
                    {
                        "correlation_id": correlation_id,
                        "authenticated_tenant_id": getattr(
                            request.state, "authenticated_tenant_id", None
                        ),
                        "method": request.method,
                        "path": request.url.path,
                        "status_code": response.status_code,
                        "duration_ms": round(duration_ms, 2),
                    }
                ),
            )
            return response
        finally:
            structlog.contextvars.clear_contextvars()

    @app.exception_handler(DomainException)
    async def handle_domain_exception(request: Request, exc: DomainException) -> JSONResponse:
        correlation_id = getattr(request.state, "correlation_id", "unknown")

        # F-062: HTTP status comes from the exception's own contract; the
        # public body carries only the stable code + generic message. The
        # internal message and details stay in server-side logs.
        status_code = exc.http_status_code or status.HTTP_500_INTERNAL_SERVER_ERROR
        if isinstance(exc, ValidationError):
            status_code = status.HTTP_400_BAD_REQUEST
        elif isinstance(exc, UnauthorizedError):
            status_code = status.HTTP_403_FORBIDDEN
        elif isinstance(exc, EntityNotFoundError):
            status_code = status.HTTP_404_NOT_FOUND
        elif isinstance(exc, (ConcurrencyError, ConflictError)):
            status_code = status.HTTP_409_CONFLICT

        code = exc.error_code or exc.__class__.__name__
        body = public_error(code, status_code)
        logger.warning(
            f"Domain exception mapped to HTTP {status_code}: {code}",
            extra=sanitize_extra(
                {
                    "correlation_id": correlation_id,
                    "exception_type": exc.__class__.__name__,
                    "error_code": code,
                    "internal_message": exc.message,
                    "internal_details": exc.details,
                    "status_code": status_code,
                }
            ),
        )
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    **body,
                    "correlation_id": correlation_id,
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_exception(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        correlation_id = getattr(request.state, "correlation_id", "unknown")
        # F-062/F-063: echo of caller input is truncated; never reflect raw
        # bodies unbounded into responses or logs.
        details = sanitize_extra({"fields": exc.errors()})
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "ValidationError",
                    "message": "Invalid request parameters or payload",
                    "details": details,
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
