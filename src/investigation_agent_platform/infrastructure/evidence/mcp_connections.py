# src/investigation_agent_platform/infrastructure/evidence/mcp_connections.py
"""Derive MCP server entries from Spring datasource config (P3-3).

Bridge between ``integrations/spring_config`` (JDBC URL parsing with
credentials stripped) and MCP server registration. Passwords NEVER flow
through here — connection definitions carry kind/host/database/username
(non-secret identity); secrets resolve exclusively through the SecretStore
port at container start (see ``docker/*/secrets`` pattern).
"""

from __future__ import annotations

import logging
from typing import Any

from investigation_agent_platform.integrations.spring_config import parse_jdbc_url

logger = logging.getLogger(__name__)

# Datasource kind -> MCP server image + query tool. crystaldba is pinned;
# Oracle SQLcl images resolve per deployment via config.
KIND_DEFAULTS: dict[str, dict[str, str]] = {
    "postgres": {
        "image": "crystaldba/postgres-mcp:0.3.0",
        "query_tool": "execute_sql",
        "access_mode": "restricted",
        "transport": "sse",
    },
    "oracle": {
        "image": "oracle-sqlcl-mcp:25.2",
        "query_tool": "run-sql",
        "transport": "stdio",
    },
}


def derive_connection_entry(
    spring_url: str,
    username: str = "",
    *,
    tenants: list[str] | None = None,
    transport: str | None = None,
    base_url: str = "",
    name: str = "",
) -> dict[str, Any]:
    """Build an MCP server entry dict from a Spring ``spring.datasource.url``.

    Returns registry-ready entry (name, kind, host, database, username,
    transport, query_tool, tenants). Raises ValueError on unparsable URLs.
    Secrets are never included — the caller injects them via SecretStore.
    """
    kind, host, database = parse_jdbc_url(spring_url)
    if not kind or not host:
        raise ValueError(f"Cannot derive MCP connection from URL: {spring_url[:40]}...")
    defaults = KIND_DEFAULTS.get(kind, {})
    entry_name = name or f"{database or kind}-mcp"
    return {
        "name": entry_name,
        "kind": kind,
        "host": host,
        "database": database or "",
        "username": username,
        "transport": transport or defaults.get("transport", "stdio"),
        "query_tool": defaults.get("query_tool", "run-sql"),
        "access_mode": defaults.get("access_mode", "restricted"),
        "image": defaults.get("image", ""),
        "base_url": base_url,
        "tenants": list(tenants or []),
    }


def derive_from_spring_datasource(
    datasource: dict[str, Any], *, tenants: list[str] | None = None
) -> dict[str, Any] | None:
    """Derive from a Spring datasource property mapping.

    Expects ``url`` (spring.datasource.url) and optional ``username``. Returns
    None (with a warning, never an exception) when the mapping has no usable
    URL — registration callers skip Nones.
    """
    url = str(datasource.get("url", "") or "")
    if not url:
        logger.warning("Spring datasource has no url; skipping MCP derivation")
        return None
    try:
        return derive_connection_entry(
            url,
            str(datasource.get("username", "") or ""),
            tenants=tenants,
            base_url=str(datasource.get("mcp_base_url", "") or ""),
            name=str(datasource.get("mcp_name", "") or ""),
        )
    except ValueError as exc:
        logger.warning("Spring datasource URL not derivable; skipping", extra={"error": str(exc)})
        return None
