# IAP Issues — Work In Progress (2026-09-27)

The single live issues file. Supersedes all archived trackers (`docs/archive/`). Status sweep 2026-09-27 (build): §1.1, §1.2, §2.1, §2.3, §3.1, §3.2 implemented and verified; remaining work is §2.2 parked triggers, §4 deferred triggers, plus new follow-ups noted below.

## 1. Open — Layer 3 wiring

### 1.1 Compose `AsyncEvidenceGateway` into `AppContext` (ex Part 5 ISSUE-8) — DONE

Implemented per `docs/IAP-implementation-part3-knowledge-v1.0-addendum.md`: new `EvidenceConfig` (`IAP_ELASTICSEARCH_URL`, `IAP_ORACLE_DSN`, timeouts), `_wire_evidence_gateway` in bootstrap (per-provider registration with fail-closed skips, mandatory sanitizer/policy, tenant-scoped authorizer/registry, frozen selector), hop resolver armed when a gateway exists. Composition testing also fixed two real drifts: `AsyncElasticAdapter` lacked `get_runtime_evidence` (added, with honest GET provenance) and `PyGit2Adapter` lacked `compare_versions` (added, bounded). Follow-up: Oracle registration needs the `oracledb` package (absent → key stays unregistered); MCP needs a server-params config shape.

### 1.2 Cognee port-level reads (ex Part 8 verdict follow-up) — DONE

Implemented per `docs/IAP-implementation-part8-knowledge-v1.0-addendum.md`: ownership populates at project time (from registration, never manufactured), `CogneeAttributionReader` implements the port contract (deterministic tie-break, ambiguity raise, projection-scoped snapshot IDs), benchmark extended with call-chain/blast-radius/sql-hop cases (7/7), live parity proven (reader returns the same symbol + ownership as the Neo4j path). Remaining: flip production reads per-application post-verdict (quotas + comparison report).

## 2. Open — Tenant and platform follow-ups

### 2.1 TENANT-1 Postgres application profiles — DONE (DRAFT)

`scripts/provision_tenant.py` saves DRAFT profiles from the ingest manifest: code section grounded per repo (locator, branch, detected language, observed source roots, repository linkage), observability/state as explicit TBD placeholders. Live: 47 DRAFT rows for TENANT-1. Fixed en route: session-factory (not raw engine) for `rls_session`, `SET LOCAL` bind-parameter syntax (validated-literal inlining), migration 007 widening `application_profiles.id` to 128. Activation (ACTIVE status + real indices/tables/templates) needs operator facts.

### 2.2 Struck API routes, parked with triggers

`evidence/{id}/source` (needs payload-store handle on `AppContext`), `graph` (needs correlation-repo handle), `request-verification` (needs per-hypothesis trigger service), `events` SSE (needs streaming infra — trigger: UI demand), applications CRUD (operator-managed for now). Each struck with rationale in the Part 4 doc; implement on trigger, not speculatively.

### 2.3 `ARCHIVED` dead branch in investigation repository — DONE

Collapsed to a single unknown-status → CANCELLED mapping with the rationale recorded (no ARCHIVED state on `InvestigationStatus`; `ProfileStatus.ARCHIVED` is unrelated).

## 3. Open — Hygiene

### 3.1 `scripts/setup.py` lint — DONE (file deleted)

Stale scaffolding with no references; removed via `git rm`. Tree-wide `ruff check` is clean.

### 3.2 Activity fail-closed audit (ex TODO P1-001) — DONE

Audited: every outer activity handler raises typed `ApplicationFailure` via the F-056 taxonomy (unknown → non-retryable); the two degraded inner paths (checkpoint hydration, snapshot build) warn explicitly with minimal fallbacks — deliberate, not silent. Hardened `create_investigation_activity` (logger line moved inside `try` so malformed input takes the typed path). Tests: taxonomy suite plus new fail-closed tests (create raises, checkpoint returns explicit `success=False` envelope). Sep-2025 claim superseded.

## 4. New follow-ups from this implementation pass

- Oracle SQLcl MCP container: rebased on the official image (`container-registry.oracle.com/database/sqlcl`, digest-pinned, SQLcl 26.2.2) after proving the vendored zip hollow. Verified: build, non-root user, Java 17, version display, clean mount-missing errors, `sql -mcp` startup. Remaining is enterprise-side: encryption key, `CONNMGR TEST`, live read + negative tests, read-only grants. Note: connection user is TOLAMOWNER (schema owner) — prefer a dedicated `MCP_INVESTIGATION_READ` account before production.
- `EvidenceStorePort` and `CorrelationExpander` are still unwired on `AppContext` — these unblock the parked `evidence/{id}/source` and `graph` endpoints (§2.2).
- Cognee production read flip per-application with quotas, after the comparison report (§1.2 remainder).

## 5. Deferred triggers (do not implement without the trigger)
GitHub App + scheduled sync (second org / weekly re-ingest); multi-branch mining (`TOPOLOGY_SNAPSHOT_NOT_READY` on non-default revision); parallel-repo ingest (p95 org > 2h); Cognee vectors (retrieval proven the bottleneck); FalkorDB (licensing clearance + core adapter); cross-repo same-dataset paths (`INCONCLUSIVE` on trace hops from coverage, not evidence); Cognee multi-user mode (isolation finding); Enterprise/Aura (noisy-neighbor pain); pygit2 hosted-packaging legal review.

## 6. Superseded archive notes (no action)

Sep-2025 TODOs P0-002 (rls_session now wired in repositories), P1-004 (in-memory repos partition by tenant), P1-005 (JWT verification with JWKS exists in `api/tenant.py`), P1-006 (traversal centralized in validator), TODO_LLM_001 (gateway factory + adapters + coordinator wiring + `test_llm_gateway.py` all present), ISSUES_0926 risks 2/3/4 (retention janitor, hop logic, CODEOWNERS fallback all implemented), coverage ≥85% (suite now 490+ passing). Re-audit before acting on any of these; do not reopen from the archived text alone.
