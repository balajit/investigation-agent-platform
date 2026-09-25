# ADR-004: MCP as Adapter Layer

**Status**: Accepted

**Context**: MCP gives generic tool access to LLMs but is not the only provider protocol the Evidence Gateway must support (REST, gRPC, and SDKs also exist).

**Decision**: Model Context Protocol (MCP) is positioned as an infrastructure adapter beneath the `Evidence Gateway` ports, not as core architecture.

**Rationale**: Prevents framework lock-in and allows standard REST/gRPC/SDK adapters alongside MCP. The domain and application layers depend only on the evidence-provider ports, never on `mcp` itself.