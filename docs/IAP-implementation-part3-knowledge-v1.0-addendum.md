# Addendum to Part 3 v1.0: Evidence Gateway Composition into `AppContext`

Status: design approved for implementation. Parent: `docs/IAP-implementation-part3-knowledge-v1.0.md` (pointer added there). Implements WIP §1.1 and arms ex-Part-5 ISSUE-5 hopping.

## Problem

`AsyncEvidenceGateway` exists with its full constructor contract, but no composition root ever builds it: activities reach it only through `getattr(ctx, "evidence_gateway", None)` fallbacks, and `GatewayTraceHopResolver` has no production instance. Everything runs on direct-adapter paths, so gateway guarantees (authorization, sanitization, provenance, persistence, bounded execution) are bypassed in production while documented as mandatory.

## Composition design

New `_wire_evidence_gateway(ctx, config)` in `bootstrap/__init__.py`, called from `_build_production_context` after repositories exist and before the topology wiring (attribution arming needs the gateway object). Construction order inside the function:

1. **Selector + provider registration**: `EvidenceProviderSelector()`; register `AsyncElasticAdapter`, `AsyncOracleStateAdapter`, `PyGit2Adapter`, and `McpEvidenceAdapter` each only when its connection details are configured — no Elastic client construction or connection config exists anywhere today, so this step adds `IAP_ELASTICSEARCH_URL` (and Oracle DSN equivalents) to config first; unconfigured providers stay unregistered, never stubbed. `PyGit2Adapter` uses `IAP_CODE_REPO_BASE` (already configured). Register, then `selector.freeze()`.
2. **Mandatory guards**: `SensitiveDataRedactor()` (sanitizer) and `QuerySafetyPolicy()` (query policy) — constructor raises without both, which is the desired fail-hard.
3. **Tenant-scoped services**: `ProfileBasedActionAuthorizer(ctx.profile_repo)` + `ProfileBasedCapabilityRegistry(ctx.profile_repo)`; `evidence_repository=ctx.evidence_repo`; telemetry from the already-wired observability adapter.
4. **Gateway**: `AsyncEvidenceGateway(provider_selector, query_safety_policy, sanitizer, evidence_store=None, correlation_engine=None, telemetry, timeout_seconds=30.0, authorizer, capability_registry, evidence_repository, repository_registry=ctx.repository_registry)`; assign `ctx.evidence_gateway`. (`evidence_store` and `correlation_engine` stay `None` until their own composition lands — see follow-ups. Tier-2 offload and graph traversal keep their current direct paths.)
5. **Hop arming**: construct `GatewayTraceHopResolver(ctx.evidence_gateway)` and pass as `trace_hop_resolver` into the existing `FailureAttributionService` construction in `_wire_topology_dependencies` (same file). No signature changes on the service.

Dev/in-memory contexts are untouched: `AppContext.__init__` sets no gateway, `getattr` fallbacks keep working, and nothing in `api/dependencies.py` changes. Production with unconfigured providers degrades per-provider (unregistered keys), never whole-gateway — except sanitizer/policy construction failure, which aborts boot (F-001 precedent, matching the gateway's own constructor).

## Failure modes

Provider client construction failure (bad Elastic URL, missing Oracle client) fails that registration with a named error and continues wiring the rest, logging which keys are absent; the gateway is still assigned. Total gateway construction failure (sanitizer/policy) raises `PlatformConfigurationError` and aborts boot. Activities keep their `getattr` guards, so a partially-wired gateway degrades exactly as today, one provider at a time.

## Tests

- Composition test (mocked clients): production builder assigns `ctx.evidence_gateway` with frozen selector; MCP absent without params; sanitizer/policy always present.
- Gateway-routed attribution integration test: seed in-memory topology + providers, attribute through the composed gateway, assert provenance envelope carries gateway markers.
- Hop test: with resolver armed, a ROUTE-boundary fixture + trace evidence produces a `CrossRepositoryHop`; without trace evidence the result is unchanged single-repo.

## Rollout

Land behind no flag (composition only; behavior changes only where fallbacks previously engaged). Verify on dev AppContext first (no gateway → identical behavior), then production-path test with mocked provider clients, then live Elastic/Neo4j verification. Follow-ups (not this addendum): `EvidenceStorePort` wiring for tier-2 offload and evidence-source fetch; `CorrelationExpander` wiring for the graph endpoint.
