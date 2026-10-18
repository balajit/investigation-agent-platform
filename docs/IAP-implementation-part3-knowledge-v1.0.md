# IAP Implementation Part 3 v1.0: Evidence Gateway, Adapters, Correlation, MCP

Supersedes `docs/archive/IAP-implementation-part3-py-v1.md` (archived for its stale §3 package tree; intent retained, layout follows the repo). Verified against code 2026-09-27. Living issues: `docs/IAP-issues-0927-wip.md`.

## Facts (current platform)

`AsyncEvidenceGateway` (`application/evidence/gateway.py`) is the mandatory enforcement boundary — authorization, sanitization, dedup, provenance, persistence, bounded execution — in front of all providers, with `EvidenceProviderSelector` routing. Adapters (`infrastructure/evidence/`): `AsyncElasticAdapter` (runtime logs/traces), `AsyncOracleStateAdapter` (SELECT-only templates via `SqlglotTemplateValidator`), `PyGit2Adapter` + `TreeSitterParser` + `TreeSitterCodeIntelligenceProvider` + `MicroSymbolResolver` + `CodeownersResolver` (code), `McpEvidenceAdapter` (MCP strictly as adapter). `CodebaseGraphPipeline` + `CodeSymbolGraphBuilder` build ephemeral `rustworkx` graphs with `DEFINES`/`CONTAINS`/`CALLS` edges and `ACCESSES_TABLE` linkage from DDL/ORM evidence. Correlation: `EphemeralCorrelationEngine` hydrated per activity from `PostgresRelationshipRepository` (authoritative edge store). All blocking C extensions (`pygit2`, `tree-sitter`, Presidio) run via `asyncio.to_thread()`.

Constraints held: no dynamic SQL from the LLM, no direct vendor calls, no shell, profiles secret-free, payloads capped and sanitized at ingress. Tenant allowlists + path-traversal guards on every filesystem read. Package root is `src/investigation_agent_platform/`; there are no `normalization/`, `caching/`, `registry.py`, or `analysis_service.py` modules (dedup/compression live inline; code intelligence is provider-based).

Addendum: gateway composition into `AppContext` (including provider registration, dev fallbacks, and hop arming) is designed in `docs/IAP-implementation-part3-knowledge-v1.0-addendum.md` — read it before touching `bootstrap/`.

Addendum: Oracle SQLcl MCP registration for multiple databases and multiple schemas per database (topology, adapter work, sanctioned dynamic-SQL exception, wiring, security, rollout) is designed in `docs/IAP-implementation-part3-knowledge-v1.0-addendum-oracle-mcp.md` — read it before registering any `oracle-mcp-*` provider key.

## Decisions kept

Reasoning never touches infrastructure; correlation is deterministic graph rules, never LLM inference; `rustworkx` is ephemeral compute, Postgres is truth; tenant+investigation scope every call.

## Future extensions

- **Manageability**: compose `AsyncEvidenceGateway` into `AppContext` (tracked open issue — activities currently degrade to direct-adapter paths where it is absent).
- **Scalability**: provider-level circuit breakers and per-tenant rate limits at the gateway; Redis-backed caching layer (specified `cashews` option, never built).
- **Performance**: Elastic scroll/point-in-time for large windows; parallel provider fan-out with per-provider timeouts inside the bounded activity.
