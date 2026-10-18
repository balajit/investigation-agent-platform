# ADR-002: Evidence as First-Class Domain Object

**Status**: Accepted

**Context**: Providers return heterogeneous payloads (log records, DB rows, source code, traces) that the reasoning layer would otherwise have to interpret.

**Decision**: All logs, database records, and code snippets are normalized into uniform `Evidence` domain objects carrying a structured `content`, `provenance`, and `freshness`.

**Rationale**: LLM reasoners consume a stable semantic model rather than vendor-specific response formats. This also gives the sanitization layer a single surface where redaction and provenance bookkeeping are applied.