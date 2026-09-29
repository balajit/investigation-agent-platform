# src/investigation_agent_platform/mcp/server.py
"""MCP investigation server: construction, transports, entry point (Part 9 Phase 6).

One tool/application layer serves both transports identically:

- **stdio** (local agent integration): single-investigation scope bound from
  environment at startup; no per-request authentication exists on stdio.
- **Streamable HTTP** (remotely hosted): per-request JWT verification plus a
  validated investigation header, enforced in pure-ASGI middleware *outside*
  the SDK dispatch so no tool ever runs unauthenticated.

The server exposes exactly the three investigation tools. It never touches
Elasticsearch, SQL parsing, or tenant authorization directly — handlers call
application services, which call the evidence abstractions.
"""

import asyncio
import logging
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from mcp.server.mcpserver import MCPServer
from starlette.responses import JSONResponse

from investigation_agent_platform.application.investigation.composition import (
    InvestigationServices,
)
from investigation_agent_platform.infrastructure.configuration.config import McpConfig
from investigation_agent_platform.mcp.context import (
    McpRequestContext,
    bind_mcp_context,
    reset_mcp_context,
    sanitize_correlation_id,
    stdio_context_from_env,
)
from investigation_agent_platform.mcp.middleware import StrictToolArgumentsMiddleware
from investigation_agent_platform.mcp.registry import (
    TOOL_ARGUMENT_PROPERTIES,
    register_investigation_tools,
)

logger = logging.getLogger(__name__)

MCP_HTTP_PATH = "/mcp"


def build_mcp_server(
    services: InvestigationServices, server_name: str = "investigation-agent-platform"
) -> MCPServer:
    """Construct the investigation MCP server with tools and strict arguments."""
    server: MCPServer = MCPServer(
        name=server_name,
        instructions=(
            "Bounded, typed, provenance-aware investigation capabilities over the "
            "current authenticated investigation. Results are scoped to the "
            "authorized tenant and investigation; pagination cursors work only "
            "with the query that produced them; truncated results say so."
        ),
        middleware=[StrictToolArgumentsMiddleware(TOOL_ARGUMENT_PROPERTIES)],
    )
    register_investigation_tools(server, services)
    return server


async def _resolve_http_context(
    tenant_header_value: str | None,
    authorization: str | None,
    investigation_header_value: str | None,
    correlation_header_value: str | None,
) -> McpRequestContext:
    """Verify HTTP transport credentials into a trusted request context."""
    from investigation_agent_platform.api.tenant import resolve_identity_from_headers

    try:
        identity = await resolve_identity_from_headers(tenant_header_value, authorization)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid authentication") from exc
    if not investigation_header_value:
        raise HTTPException(status_code=400, detail="Missing investigation header.")
    try:
        investigation_id = UUID(investigation_header_value.strip())
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail="Malformed investigation header.") from exc
    return McpRequestContext(
        tenant_id=identity.tenant_id,
        investigation_id=investigation_id,
        correlation_id=sanitize_correlation_id(correlation_header_value),
        actor_id=identity.principal_id,
    )


class McpAuthMiddleware:
    """Pure-ASGI transport authentication around the Streamable HTTP app.

    Verifies every request before SDK dispatch; failures are plain HTTP
    statuses (401/400/403), never MCP results. Pure ASGI (no threadpool hop)
    so the bound context propagates to tool execution in the request task.
    """

    def __init__(
        self,
        app: Any,
        *,
        tenant_header: str = "X-Tenant-ID",
        investigation_header: str = "X-Investigation-ID",
    ) -> None:
        self._app = app
        self._tenant_header = tenant_header.lower().encode("latin-1")
        self._investigation_header = investigation_header.lower().encode("latin-1")
        self._authorization_header = b"authorization"
        self._correlation_header = b"x-correlation-id"

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))

        def _header(name: bytes) -> str | None:
            raw = headers.get(name)
            if raw is None:
                return None
            try:
                decoded = raw.decode("latin-1")
            except (UnicodeDecodeError, AttributeError):
                return None
            return decoded if isinstance(decoded, str) else None

        try:
            context = await _resolve_http_context(
                _header(self._tenant_header),
                _header(self._authorization_header),
                _header(self._investigation_header),
                _header(self._correlation_header),
            )
        except HTTPException as exc:
            response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
            await response(scope, receive, send)
            return
        token = bind_mcp_context(context)
        try:
            await self._app(scope, receive, send)
        finally:
            reset_mcp_context(token)


def create_mcp_http_app(services: InvestigationServices, mcp_config: McpConfig) -> Any:
    """Build the authenticated Streamable HTTP ASGI app (mount or serve)."""
    server = build_mcp_server(services, server_name=mcp_config.server_name)
    inner = server.streamable_http_app(streamable_http_path=MCP_HTTP_PATH)
    return McpAuthMiddleware(
        inner,
        tenant_header=mcp_config.tenant_header,
        investigation_header=mcp_config.investigation_header,
    )


def mount_mcp(app: Any, services: InvestigationServices, mcp_config: McpConfig) -> None:
    """Mount the authenticated MCP app onto a FastAPI application."""
    app.mount(MCP_HTTP_PATH, create_mcp_http_app(services, mcp_config))
    logger.info("MCP investigation service mounted", extra={"path": MCP_HTTP_PATH})


async def _run_stdio_server(services: InvestigationServices, mcp_config: McpConfig) -> None:
    context = stdio_context_from_env(
        mcp_config.stdio_tenant_id,
        mcp_config.stdio_investigation_id,
        mcp_config.stdio_actor_id,
    )
    server = build_mcp_server(services, server_name=mcp_config.server_name)
    token = bind_mcp_context(context)
    try:
        logger.info(
            "MCP stdio server starting",
            extra={
                "context": {
                    "tenant_id": context.tenant_id,
                    "investigation_id": str(context.investigation_id),
                }
            },
        )
        await server.run_stdio_async()
    finally:
        reset_mcp_context(token)


def run_mcp_stdio(services: InvestigationServices, mcp_config: McpConfig) -> None:
    """Run the MCP server over stdio (blocking)."""
    asyncio.run(_run_stdio_server(services, mcp_config))


def main(argv: list[str] | None = None) -> int:
    """Module entry point: stdio from a fully composed production context."""
    from investigation_agent_platform.bootstrap import build_app_context
    from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError
    from investigation_agent_platform.infrastructure.configuration.config import (
        load_application_config_from_env,
    )

    try:
        config = load_application_config_from_env()
    except PlatformConfigurationError as exc:
        print(f"Invalid configuration: {exc.message}")
        return 2
    if not config.mcp.enabled:
        print("MCP server is disabled (IAP_MCP_ENABLED=true to enable).")
        return 2
    if config.mcp.transport not in ("stdio", "streamable-http", "http"):
        print(f"Unsupported MCP transport: {config.mcp.transport}")
        return 2
    context = build_app_context(config)
    services = getattr(context, "investigation_services", None)
    if services is None:
        print("Production context has no investigation services; cannot serve MCP.")
        return 2
    if config.mcp.transport == "stdio":
        run_mcp_stdio(services, config.mcp)
        return 0
    # HTTP runs under uvicorn so the authentication middleware stays in force;
    # the SDK standalone runner cannot apply it.
    import uvicorn

    uvicorn.run(
        create_mcp_http_app(services, config.mcp),
        host=config.mcp.host,
        port=config.mcp.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
