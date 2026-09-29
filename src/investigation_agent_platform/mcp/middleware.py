# src/investigation_agent_platform/mcp/middleware.py
"""MCP server middleware: strict tool arguments (Part 9 Phase 6).

The SDK validates argument *types* but silently drops unknown properties.
This middleware rejects unknown properties at the protocol tier (before
dispatch) with a JSON-RPC invalid-params error, so the tool contract's
``additionalProperties: false`` holds observably. Semantic validation
(values, scopes, cursors) stays in the handlers, which return the
structured error envelope.
"""

import logging
from typing import Any

from mcp.shared.exceptions import MCPError

logger = logging.getLogger(__name__)

_INVALID_PARAMS_CODE = -32602


class StrictToolArgumentsMiddleware:
    """SDK ``ServerMiddleware`` rejecting unknown tool arguments."""

    def __init__(self, allowed_properties: dict[str, frozenset[str]]) -> None:
        self._allowed = allowed_properties

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        if getattr(ctx, "method", None) == "tools/call":
            params = getattr(ctx, "params", None)
            name: Any = None
            arguments: Any = None
            if isinstance(params, dict):
                name = params.get("name")
                arguments = params.get("arguments", {})
            else:
                name = getattr(params, "name", None)
                arguments = getattr(params, "arguments", None) or {}
            if isinstance(name, str) and name in self._allowed and isinstance(arguments, dict):
                unknown = sorted(set(arguments) - self._allowed[name])
                if unknown:
                    logger.warning(
                        "MCP tool call rejected: unknown arguments",
                        extra={"context": {"tool": name, "unknown": unknown}},
                    )
                    raise MCPError(
                        _INVALID_PARAMS_CODE,
                        f"Unknown arguments for tool '{name}': {', '.join(unknown)}.",
                    )
        return await call_next(ctx)
