# src/investigation_agent_platform/mcp/registry.py
"""Deterministic MCP tool registration (Parts 9 Phase 6, 11.6).

Investigation tools plus the investigation-scoped reference search — no
infrastructure administration surface. Registration holds no logic;
handlers close over the application services. ``EXPECTED_TOOLS`` is the
contract-test anchor: every expected tool exists, no unexpected tool exists.
"""

from mcp.server.mcpserver import MCPServer

from investigation_agent_platform.application.investigation.composition import (
    InvestigationServices,
)
from investigation_agent_platform.mcp.tools.evidence_detail import (
    TOOL_NAME as GET_EVIDENCE_TOOL_NAME,
)
from investigation_agent_platform.mcp.tools.evidence_detail import (
    register_get_evidence_tool,
)
from investigation_agent_platform.mcp.tools.reference_docs import (
    TOOL_NAME as SEARCH_REFERENCE_DOCS_TOOL_NAME,
)
from investigation_agent_platform.mcp.tools.reference_docs import (
    register_search_reference_docs_tool,
)
from investigation_agent_platform.mcp.tools.runtime_evidence import (
    TOOL_NAME as SEARCH_RUNTIME_EVIDENCE_TOOL_NAME,
)
from investigation_agent_platform.mcp.tools.runtime_evidence import (
    register_search_runtime_evidence_tool,
)
from investigation_agent_platform.mcp.tools.trace_investigation import (
    TOOL_NAME as INVESTIGATE_TRACE_TOOL_NAME,
)
from investigation_agent_platform.mcp.tools.trace_investigation import (
    register_investigate_trace_tool,
)

EXPECTED_TOOLS: frozenset[str] = frozenset(
    {
        SEARCH_RUNTIME_EVIDENCE_TOOL_NAME,
        GET_EVIDENCE_TOOL_NAME,
        INVESTIGATE_TRACE_TOOL_NAME,
        SEARCH_REFERENCE_DOCS_TOOL_NAME,
    }
)

# Exact argument surface per tool. The SDK validates types and constraints;
# the strict-arguments middleware rejects anything outside these sets, so a
# raw-DSL/index/script parameter is structurally inexpressible.
TOOL_ARGUMENT_PROPERTIES: dict[str, frozenset[str]] = {
    SEARCH_RUNTIME_EVIDENCE_TOOL_NAME: frozenset(
        {
            "environment",
            "identifiers",
            "keywords",
            "services",
            "severities",
            "start_time",
            "end_time",
            "limit",
            "cursor",
        }
    ),
    GET_EVIDENCE_TOOL_NAME: frozenset({"evidence_id"}),
    INVESTIGATE_TRACE_TOOL_NAME: frozenset(
        {"trace_id", "environment", "start_time", "end_time", "max_evidence"}
    ),
    SEARCH_REFERENCE_DOCS_TOOL_NAME: frozenset({"q", "kinds", "limit", "cursor"}),
}


def register_investigation_tools(server: MCPServer, services: InvestigationServices) -> None:
    """Register the investigation tools on a server instance."""
    register_search_runtime_evidence_tool(server, services)
    register_get_evidence_tool(server, services)
    register_investigate_trace_tool(server, services)
    register_search_reference_docs_tool(server, services)
