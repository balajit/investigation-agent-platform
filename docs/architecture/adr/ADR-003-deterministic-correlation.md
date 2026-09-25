# ADR-003: Deterministic Entity Correlation

**Status**: Accepted

**Context**: Correlating entities across evidence items (e.g. `sessionId`, `traceId`, `requestId`) by LLM inference is non-deterministic and costly.

**Decision**: Identifiers (`sessionId`, `traceId`, `requestId`) are correlated deterministically via graph rules rather than LLM inference. A deterministic correlation engine traverses persistence-hydrated relationships with exact key matching.

**Rationale**: Guarantees 100% precision for structural identity links without relying on non-deterministic LLM output, and keeps correlation latency low enough for the live evidence-generation loop.