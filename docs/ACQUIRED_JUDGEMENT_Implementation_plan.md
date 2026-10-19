# Acquired Judgement — Implementation Plan

Companion to `docs/ACQUIRED_JUDGEMENT.md` (the why). This is the what, in order.
Conventions hold across all phases: respondent-free committed content (rule),
deterministic outputs (sorted, pinned inputs, normalized paths), fail-closed
gates over fail-open ingestion, idempotent writes, ledger entries per phase.

## Phase 0 — Foundations (no user-visible change)

- **Content-hash key helpers**: `sha256` over normalized file bytes + canonical
  `(repo, SHA, stage)` result keys, shared utility module. Everything downstream
  keys off these.
- **Sidecar JSON contracts** (`analyzer/v1`): file/symbol/endpoint/edge record
  shapes with schema validation at ingestion boundaries.
- **Synthetic fixtures**: extend `scripts/build-sample-checkout.py` with a
  second module + known FK/edge shapes so every later phase has golden data
  that never touches respondent systems.
- **Acceptance**: helpers unit-tested; fixture builds reproducibly; full suite green.
- **Non-goals**: no behavior change anywhere.

## Phase 1 — Batch inventory (Stage 1 sidecars → store)

- Wire **cdxgen** (manifests → CycloneDX JSON → dependency edges, shared-lib
  fan-out, license data), **OWASP Noir** (endpoint sweep → normalized endpoint
  records; hand parsers become validators), **tree-sitter fast pass** (file/
  symbol/annotation inventory + generated-code flags via markers, paths,
  logic-density heuristic) into Postgres (`analyzer_*` tables keyed by
  repo+SHA).
- `RepositoryAnalyzer` processors (manifest, endpoints, generated, auth,
  model-tier, edge-emitter) implemented as **store queries**, not file walks.
- Initial onboarding + SHA-change re-analysis triggers (manual first; sync
  wiring in Phase 6).
- **Acceptance**: 120-repo-shaped synthetic + sampled-real inventory completes;
  generated share measured; edge yield spot-checked; golden service records pass.
- **Non-goals**: Spoon resolution, live queries against the store.

## Phase 2 — Domain hubs + class ranking (the PoC, productized)

- **DB extractor** (Oracle `ALL_CONSTRAINTS`/`ALL_CONS_COLUMNS`/`ALL_TABLES`,
  Postgres `information_schema`/`pg_class`), schema-scoped, dialect-routed.
  Dormant-edge flagging (empty child tables), verified `COUNT(*)` on hub
  candidates, fingerprint (`COUNT` + constraint hash) for rebuild gating.
- **Class ranker**: import fan-in, HITS-authority, entity-package prior,
  churn, test-refs, instantiation, ubiquity cross-match, generic stoplist;
  split output (hubs / vocabulary / communities); `@Table` map + stem fallback
  + operator aliases for the class→table bridge.
- **Witness merge**: DB × code agreement scoring; disagreement reported.
- **Hub lookup primitive** + `domain_hubs` into profile build (merge derived
  slot) + orientation block + verify corroboration (per §§4–5 of the design doc).
- **Acceptance**: hub list reproduces PoC ranking on live data; characteristic→hub
  walk completes; fingerprint proves no-rebuild on repeats; unit + live proof.
- **Non-goals**: runtime-usage ranking (awaits catalog-role grant), LDA/ownership.

## Phase 3 — Acquired judgement memory (associations + mid-loop facts)

- **Association records** `{symptom_signature, hub, cause, fix_locations,
  outcome, weight}`: deterministic capture on VERIFIED (zero extra LLM cost),
  miss records on refutation, reinforcement on verify events, janitor decay.
- **Mid-loop banking**: settled statements banked per verify turn (prose-first,
  triple-attached, provenance-bound); next iteration reasons over them.
- **Jump-start packet**: investigation-start recall (symptom → hub → ranked
  associations + ruled-out branches) as orientation; per-tenant, fingerprint-proof
  sharing only.
- **Trust calibration**: orientation-only above threshold; validator still
  demands live citations. Memory suggests, evidence decides.
- **mem0 lane decision** (from open thread §8): Postgres-only unless real
  human-context content is identified; mem0 kept behind flag regardless.
- **Acceptance**: repeat symptom class shows iteration-0 hub-directed start and
  reduced turns-to-verify vs baseline; refuted paths not re-explored; weights
  move in the right direction on verify/refute events.
- **Non-goals**: new ActionType, critic coupling.

## Phase 4 — Customer context assembly

- **Standing profile**: customer (account/segment/lifecycle → Contract-hub) +
  sales profile (offers/entitlements → Offer/Catalog-hub) via state providers,
  new declarations reusing datasource + tenant-gate patterns.
- **Trail fusion**: journey spine + session-entity picks interpreted against
  the standing profile; hub mapping with the **mismatch detector**
  (behavior contradicting entitlements = top-ranked signal).
- **Cross-session lane**: per-account recurrence/preferences, retention-bounded
  (default session TTL; bounded window only on explicit operator decision).
- **Acceptance**: entitlement-vs-behavior gap case identified as the affected
  hub end to end; session-entity coverage for cart/account tables proven.
- **Non-goals**: persistent customer profiles (assembly stays per-investigation).

## Phase 5 — Resolved edges + cross-repo queries (Spoon)

- **Spoon sidecar** (JVM, JSON out): client→endpoint links, resolved call
  edges, confidence grades (Exact/Inferred); nightly/on-demand runs.
- **Config-server snapshot source** (new datasource type): without it half the
  gateway-mediated edges are missing — required, not optional.
- **Queries**: blast radius (reverse edges), dependency depth across gateway
  hops, shared-lib fan-out (re-verify scoping), env-asymmetric diffs
  (dev-vs-prod drift evidence), platform-single-point (SAT/eureka/config-server
  one-hop causes).
- **Acceptance**: each query demonstrated live on the surveyed orgs; shared-lib
  change correctly scopes affected services.
- **Non-goals**: CodeQL, per-query parser fallback as primary mode.

## Phase 6 — Scale hardening (1000× design, built in)

- Temporal fan-out (per-repo activities; retry/idempotency/budgets for
  stragglers), differential re-analysis (O(diff)), content-addressed global
  dedup, HyperLogLog fleet stats, priority scheduling, reservoir-sampled QC
  with full goldens on synthetic fixtures, canary operator rollout.
- `sync` command subsuming graph rebuilds (closes the §7 thread).
- **Acceptance**: byte-identical outputs under reordered shards; partial-failure
  drill (dead-letter + explicit partial marking); canary promotion recorded.
- **Non-goals**: multi-region, cross-process disk cache beyond content store.

## Phase 7 — Validation, runbook, close-out

- ArchUnit-style layering gates over the built graph (optional enforcement).
- Operator runbook (onboarding, rotation, grants incl. catalog-role request,
  retention) + architecture doc sections per phase + ledger close-outs.
- `poc/` deleted per its README; scratch scripts either productized or removed.
- Final acceptance: full suite green, compliance grep zero hits, PoC claims
  re-proven against production code paths (not scratch).
