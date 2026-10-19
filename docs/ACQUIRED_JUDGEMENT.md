# Acquired Judgement: How IAP Learns to Troubleshoot Like an Engineer

Status: design record (pre-build). PoC evidence: `poc/` (scratch, deleted at build).

## 1. The observation

An experienced engineer hearing an issue does three things instantly: associate it to a domain center (product, offer, market availability), recall where that kind of trouble lived before, and go straight to the likely code blocks or data relationships (wrong/missing data). IAP today does none of this — every investigation starts from keyword soup, and nothing learned in one run helps the next beyond raw retrieved facts.

## 2. Two memories

- **Anatomy** (slow-changing): domain hubs, FK edges, class rankings. Answers "what exists and how it connects." Rebuilt on schema/release change, fingerprint-gated.
- **Acquired judgement** (per conclusion): symptom → hub → cause → fix-location associations with weights. Answers "I've seen this before — go here first."

## 3. PoC evidence (one service: 496 tables / 647 FKs / 1303 classes)

**DB hubs** (`fan_in + 0.5·fan_out + log10(rows+1)`, link-tables demoted): product table (79 in), market table (71), catalog-pub table (57), base-offer table (44), billing-code table (27). **28% of FKs (181) sit on empty child tables** — flagged dormant, excluded from scoring; hub candidates get verified `COUNT(*)` because `NUM_ROWS` is stats-based.

**Class ranking** (import fan-in + HITS authority + entity-package prior + churn + test-refs + instantiation + ubiquity cross-match, generic stoplist): Product, BaseOffer, Market top one shared community. **Complexity split found**: ~5 of top 25 are zero-method value types (enums) — scorer output must split hubs / vocabulary / communities, not one scale.

**Convergence**: DB fan-in and code fan-in independently surface the product/market/offer/billing centers. Agreement across witnesses is the confidence signal; disagreement is reported, not hidden.

**Code evaluation honesty**: zero `@Entity` in the service module (orchestration facade: controllers → rules/helpers → SOAP/cache, no SQL/DAO); JPA entities live in the shared-library module (`catalog-jpa` equivalent, `@Table`-mapped); service imports reveal service↔domain affinity (`entity.<domain>.*`, DAOs, holders). The class→table bridge is `@Table` where present, stem-match fallback, operator aliases last resort.

**Freshness model**: treesitter per-query fresh; graph cache mtime-guarded (after §51 fix); git reads commit-pinned; pinned checkouts sync-gated. No silent staleness in the code path.

**Denied**: `V$SQLAREA` needs `SELECT_CATALOG_ROLE` — runtime-usage ranking awaits that grant.

## 4. Association record design

```text
{ symptom_signature (fingerprints + hub + error patterns),
  hub_entity, root_cause_statement, fix_locations,
  outcome (VERIFIED | REFUTED), weight }
```

- **Capture**: deterministic extraction on VERIFIED conclusions (zero extra LLM cost); misses recorded too ("not in the billing-code hub" is knowledge).
- **Recall**: investigation start + new symptoms → ranked associations as orientation with weights.
- **Reinforcement**: verify events adjust weights; janitor prunes below threshold.
- **Trust calibration**: orientation only, above threshold; the validator still demands live citations. Memory suggests, evidence decides.
- **Tenancy**: per-tenant records; cross-tenant only via fingerprint-proof sharing.

## 5. Platform fit (no new actions in v1)

1. Profile build: `domain_hubs` + FK edges into the existing merge derived slot (versioned, offline).
2. Reason orientation: hub block + association notes beside refdoc orientation.
3. Verify: hub-membership corroboration in state-claim validation.
4. Refresh: fingerprint-gated rebuilds; misses recorded.

Value: fewer empty gathering loops (token/budget), multi-hop state traversal the loop cannot do today, hub-anchored claims that verify, first-class "which entity is affected" answers, cheaper honest abstention ("no hub affinity").

## 6. Algorithm verdicts

Keep: import fan-in, HITS-authority (separates prestige from brokerage — the centers-vs-characteristics split), entity-package prior, churn (living-core), test-refs (human fear), ubiquity cross-match, generic stoplist, communities (bounded-context views, not one global list). Drop: PageRank (no lift over fan-in here), TF-IDF (structural signals suffice). Deferred: LDA topics, ownership mining, per-query worktree search. Superseded by §13: content-addressed global parse cache replaces the earlier "deferred disk-backed cache" position — same idea, widened from process-local to fleet-global with identical staleness immunity.

## 7. Open threads (not decisions)

- DTO characteristic fields as hub attributes (hubs + their fields for "which table holds X").
- Alias-map home (scope seed file vs profile field).
- Graph rebuilds folded into the planned `sync` command.
- Runtime-usage ranking behind the catalog-role grant.
- `V1` deliberately excludes: new ActionType, live per-run extraction, critic coupling.

## 8. Words for the LLM (memory loop design)

System rows (investigations/hypotheses/evidences) are not LLM food — the loop must bank LLM-native prose facts mid-run so the next thinking step consumes words. Decisions pending discussion:

- **Loop order**: mid-loop compounding first (helps 100% of runs; near-zero marginal cost by reusing reason output), cross-run jump-start second (low problem recurrence makes episodic recall the smaller prize).
- **Fact shape**: prose-first for the prompt, triple-attached (`subject/predicate/object`) for deterministic matching, provenance-bound so forgetting is safe.
- **Two lanes**: deterministic technical memory (Postgres envelopes, exact, versioned) for structural ground truth; mem0 repaired specifically for assistive human-context memory (customer context, preferences, fuzzy recall) — not as the technical store. Open: whether real human-context content exists to populate the mem0 lane, or Postgres-only for now.
- **Mid-loop banking cadence**: every verify vs status-transitions only (open).

## 9. Customer context (session-scoped, then standing)

Customer context is not a persistent profile — it assembles per investigation:

1. **Standing profile** (journey start = what the customer HAS): customer profile (account, segment, lifecycle → Contract-hub rows) + sales profile (offers/entitlements owned → Offer/Catalog-hub rows). No such declaration exists yet; new assembly reusing datasource + tenant-gate patterns.
2. **Session trail** (what they did): journey spine + picked items via session keys, interpreted against layer 1.
3. **Hub mapping** (what it means): items/account/entitlements resolved to hubs. The sharpest signal: session behavior contradicting the standing profile (unentitled item, ineligible segment) — most issues are profile-vs-behavior gaps.
4. **Cross-session memory** (what happened before): mem0 lane, per-account recurrence/preferences, retention-bounded.

Ordering is also the cheap order: standing profile prunes the 496-table space before any broad query runs. Open: session-entity coverage for cart/account tables; cross-session retention window (session TTL vs bounded e.g. 90d).

## 10. Repository onboarding at scale (two orgs surveyed: ~50 + ~72 repos)

Shapes observed: capability services, generated connector repos (largest by file count, emptiest signal — JAXB-type stubs), shared libraries, platform (API gateway, OAuth wrapper, config-server, Kafka SDKs), CQRS splits, second agents in the wild. Near-uniform Java (Quarkus + Spring mixed — a technology dimension the graph carries), plus outlier runtimes.

Connection truth, verified: services call through **gateways/DNS + config-server-resolved URLs** (near-zero hardcoded endpoints in connectors; static analysis alone cannot resolve targets — config-server snapshots become an edge source). **Connector mappings** are explicit A→connector→B edges. **Shared domain-model libraries** (internal vs external) mark the visibility tier: internal models serve in-domain services, external models are peer-team contracts with different blast-radius semantics. **OAuth wrapper + SAT endpoints** form a shared auth dependency (single-hop explanation for multi-service symptoms). **Generated connectors** are indexing-excluded (keep their tests, which reference real services).

## 11. OSS verdicts (surveyed, ranked)

Adopt: **tree-sitter** (already a dependency; polyglot pre-pass, zero new infra), **cdxgen** (Apache-2.0 sidecar; Maven/Gradle/polyglot manifests → CycloneDX JSON), **OWASP Noir** (MIT; endpoint sweep to OAS3 in seconds per repo; hand parsers become validators), **Spoon** (JVM sidecar emitting JSON; the only production-grade resolver — client→endpoint links, call edges, confidence-graded Exact/Inferred). Defer: JavaParser/ArchUnit (helper + gate, not discovery), CodeQL (requires building everything), jQAssistant (GPL-3.0 + weight), ORT (compliance-suite weight), degraph/JDepend (unmaintained). Generated-code detection has no framework — heuristic stack on stored file records. No dominant microservice-graph library exists; composition over sidecar outputs is the pattern.

## 12. AST: parse once, store (decided)

Per-query parsing re-scans the world per question and never accumulates resolution. Split: **Stage 1 (batch, cheap)** — cdxgen + Noir + tree-sitter fast pass → file/symbol/endpoint JSON in Postgres, amortized once, diffable across onboardings. **Stage 2 (deep, on demand/nightly)** — Spoon sidecar → resolved edges with confidence grades. Queries hit the store, never the parser; per-file tree-sitter remains only as ad-hoc fallback. Invalidation is "re-run stage 1 on SHA change" — sync-triggered re-analysis (planned, unbuilt).

## 13. Scale architecture (one operator = 1/1000th of the target)

- **Content addressing**: key expensive artifacts by content hash, not location — shared libraries and generated stubs parsed once globally, served to every operator containing them.
- **Parallel core**: per-repo fan-out over the existing Temporal machinery (retry/idempotency/budgets do straggler handling); deterministic merge (sorted outputs, pinned sidecar digests, normalized paths) so parallelism never affects output bytes.
- **Exactly-once effects**: idempotent writes on `(repo, SHA, stage)`; retries safe; partial results explicitly marked.
- **Differential analysis**: on SHA change, diff the file set and re-parse/ invalidate only touched edges; steady-state cost O(diff), not O(repo).
- **Approximation where exactness is vanity**: HyperLogLog for fleet stats; reservoir-sampled QC per batch with full goldens on synthetic fixtures in CI.
- **Priority scheduling + per-operator budgets** (existing platform concepts): changed repos and requested scopes first, cold backfill on preemptible workers.
- **Quality contract**: provenance per result row (how derived), per-shard validation gates (fail loud, never fail-open ingestion), post-merge consistency checks (dangling edges, orphans), canary operator for pipeline changes.
- Deliberately single-machine: per-repo parsing itself and the final per-operator merge. Distribution follows data independence.

## 14. Fleet topology + cross-repo issue queries

One API+worker fleet serves all scopes (scopes are data: `scopes/<org>/<env>/<app>/` with env levers + app leaves, provenance-annotated merge, secret denylist, 0700 operator dirs). Credential resolution per request (`secret://` → SecretStore at scale). Cross-repo queries the graph unlocks: blast radius (reverse endpoint edges), dependency depth across gateway hops (extends trace logic beyond one checkout), shared-lib fan-out (which services re-verify on a library change), env-asymmetric edges (dev-vs-prod diffs as drift evidence), platform-single-point explanations (SAT/eureka/config-server as one-hop causes).
