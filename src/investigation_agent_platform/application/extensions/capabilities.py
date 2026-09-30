# src/investigation_agent_platform/application/extensions/capabilities.py
"""Capability discovery aggregation (Part 11.1).

Builds the authenticated ``GET /api/v1/capabilities`` payload from one
source of truth: registered plugin manifests plus the pinned MCP tool
contract. Only safe fields cross this boundary — ids, contract versions,
availability, modes, and limits. Never config values, endpoints, or secrets.
"""

from __future__ import annotations

from typing import Any

from investigation_agent_platform.application.extensions.registries import ExtensionRegistries

MCP_TOOL_CAPABILITY_PREFIX = "mcp_tool:"
MCP_TOOL_CONTRACT_VERSION = "1.0"


def mcp_tool_entries() -> list[dict[str, Any]]:
    """Publish the pinned MCP tool contract as discovery entries (read-only).

    Reads ``EXPECTED_TOOLS`` without altering registration behavior; the
    strict MCP contract tests remain the authority on the exact tool set.
    MCP tools are not plugins of any ``PluginKind``, so they bypass
    ``PluginManifest`` and are published directly as entries. Imported
    lazily so capability tooling never hard-depends on the MCP server
    package at module load.
    """
    from investigation_agent_platform.mcp.registry import EXPECTED_TOOLS

    return [
        {
            "capability_id": f"{MCP_TOOL_CAPABILITY_PREFIX}{name}",
            "contract_version": MCP_TOOL_CONTRACT_VERSION,
            "status": "available",
            "modes": ["invoke"],
            "limits": {},
        }
        for name in sorted(EXPECTED_TOOLS)
    ]


def list_capabilities(registries: ExtensionRegistries) -> list[dict[str, Any]]:
    """Aggregate registry manifests + MCP tools into discovery entries."""
    entries: list[dict[str, Any]] = []
    for manifest in registries.all_manifests():
        entries.append(
            {
                "capability_id": f"{manifest.plugin_kind.value}:{manifest.plugin_id}",
                "contract_version": manifest.contract_version,
                "status": "available",
                "modes": sorted(manifest.capabilities),
                "limits": {},
            }
        )
    entries.extend(mcp_tool_entries())
    entries.sort(key=lambda entry: entry["capability_id"])
    return entries
