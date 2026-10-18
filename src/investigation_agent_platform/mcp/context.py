# src/investigation_agent_platform/mcp/context.py
"""Trusted MCP request context (Part 9 Phase 6).

Tenant and investigation identity always come from transport authentication,
never from agent-controlled tool arguments (there is no such tool argument).
HTTP resolves identity per request in ASGI middleware (JWT verification plus
a validated investigation header); stdio binds one server instance to one
investigation from environment. Handlers read the ambient context and convert
it to an application ``InvestigationScope`` — an absent context is a server
misconfiguration, never an anonymous request.
"""

import logging
import os
import re
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from uuid import UUID

from investigation_agent_platform.api.errors import sanitize_extra
from investigation_agent_platform.application.investigation.context import InvestigationScope

logger = logging.getLogger(__name__)

_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(frozen=True)
class McpRequestContext:
    """Authenticated scope for one MCP tool invocation."""

    tenant_id: str
    investigation_id: UUID
    correlation_id: str
    actor_id: str | None = None

    def to_scope(self) -> InvestigationScope:
        """Convert to the application-layer trusted scope."""
        return InvestigationScope.create(
            tenant_id=self.tenant_id,
            investigation_id=self.investigation_id,
            correlation_id=self.correlation_id,
            actor_id=self.actor_id,
        )


_current_context: ContextVar[McpRequestContext | None] = ContextVar(
    "iap_mcp_request_context", default=None
)


def require_mcp_context() -> McpRequestContext:
    """Return the ambient trusted context or fail closed (server miswired)."""
    context = _current_context.get()
    if context is None:
        raise RuntimeError(
            "MCP request context is not established; the server transport "
            "must bind tenant and investigation scope before dispatch."
        )
    return context


def bind_mcp_context(context: McpRequestContext) -> Token[McpRequestContext | None]:
    """Bind an ambient context; returns the reset token for cleanup."""
    return _current_context.set(context)


def reset_mcp_context(token: Token[McpRequestContext | None]) -> None:
    """Release an ambient context binding."""
    _current_context.reset(token)


def sanitize_correlation_id(raw: str | None) -> str:
    """Accept a caller correlation ID only if safely shaped; else mint one."""
    if raw and _CORRELATION_ID_RE.match(raw):
        return raw
    return uuid.uuid4().hex


def stdio_context_from_env(
    tenant_id: str, investigation_id: str, actor_id: str | None = None
) -> McpRequestContext:
    """Build the single-scope stdio context; fail closed when unconfigured."""
    if not tenant_id or not investigation_id:
        raise RuntimeError(
            "stdio MCP transport requires IAP_MCP_TENANT_ID and "
            "IAP_MCP_INVESTIGATION_ID to bind the server scope."
        )
    try:
        investigation_uuid = UUID(investigation_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise RuntimeError("IAP_MCP_INVESTIGATION_ID is not a valid UUID.") from exc
    if not actor_id:
        # Shared-host attribution limit (TO_DO-029): stdio has no per-request
        # auth, so every tool call on a shared host attributes to one actor.
        # The default stays for local dev; anything else must set
        # IAP_MCP_ACTOR_ID explicitly. Requiring it outright would break
        # existing local-dev setups, so non-local environments get a loud
        # warning instead of a hard failure.
        environment = os.environ.get("IAP_ENVIRONMENT", "local").lower()
        if environment not in ("local", "development", "test"):
            logger.warning(
                "MCP stdio actor unattributed: IAP_MCP_ACTOR_ID is unset; "
                "falling back to the shared 'local-operator' identity",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "investigation_id": investigation_id,
                        "event": "mcp.stdio.default_actor",
                        "environment": environment,
                    }
                ),
            )
    return McpRequestContext(
        tenant_id=tenant_id,
        investigation_id=investigation_uuid,
        correlation_id=uuid.uuid4().hex,
        actor_id=actor_id or "local-operator",
    )
