# IAP Implementation Part 6 v1.0: Execution Knowledge Layer (Mem0 + Graphiti)

Supersedes `docs/archive/IAP-implementation-part6-knowledge-v1.md`. Verified against code 2026-09-27. Living issues: `docs/IAP-issues-0927-wip.md`.

## Facts (current platform)

All four slices live. Slice 0: `KnowledgeArtifact` envelope in Postgres (validity semantics `IMMUTABLE`/`CONDITIONAL`/`TTL`, `ReverifySpec` required for conditional, `TTL` requires `valid_to`, `SHARED_CODE_ISSUE` visibility restricted to static content via `TENANT_ONLY_KINDS`), `InvestigationSession` rows, tenant-agnostic code-issue fingerprinting with cross-tenant merge-or-fork intake (`POST /v1/intake/errors`, closed reopens as "recurring code issue", cancelled never resurrected, redacted cross-tenant placeholders). Slice 1: `Mem0KnowledgeStore` on pgvector (same Postgres, `CREATE EXTENSION vector`), tenant-namespaced IDs + mandatory metadata filters, verbatim path for mechanical facts. Slice 2: `GraphitiTemporalKnowledge` on shared Neo4j (server-derived group IDs, valid-time filtering, byte-cap truncation with pointers, per-group serialized ingest). Slice 3: capture/retrieve activities with validity gating (`excluded_stale_count` never silent), TTL janitor workflow, episode caps with degrade-to-envelopes, token/cost telemetry, `code_refs → Layer 3` join in retrieval views. Config: `KnowledgeConfig` (episode cap 50, semaphore 5, byte cap 8192, evidence TTL 90d), all `IAP_KNOWLEDGE_*` wired. Human APIs: per-investigation knowledge view, tenant-scoped semantic search.

## Decisions kept

Self-hosted stores (no data-control boundary change, bounded cost); envelope as validity authority (stores never invent truth); staleness structurally unignorable (re-verify or exclude + count); knowledge capture never fails an investigation.

## Future extensions

- **Manageability**: staleness dashboard on `excluded_stale_count` trends; rebuild-from-envelopes runbooks (both indexes disposable by design).
- **Scalability**: managed Zep/Mem0 Platform only on compliance/SLA/cost triggers (documented); per-tenant projection quotas under auto-forwarded volume.
- **Performance**: retrieval precision evals before any recall-model change; embedder-dimension consistency checks on upgrade.
