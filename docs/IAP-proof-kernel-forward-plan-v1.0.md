# IAP Proof-Kernel Forward Plan v1.0 — Dependency-Ordered Steps to Proceed

> Status: execution plan, not implementation authorization. Each stage still
> requires explicit owner go-ahead before code is written. Builds on
> `docs/IAP-proof-kernel-plan-v1.0.md` (master plan, gates G1–G8, WP
> definitions); where they conflict on ordering, this document governs
> sequencing only and never relaxes a gate.
> Non-goals restated: no authoritative rollout before gates pass; no
> env-switched legacy behavior (recorded versions only); no production
> wiring of kernel packages until their gate is passed.

## 0. Current state (2026-10-24, suite 3,046 green, ruff clean)

Done and gated in unit tests: WP-A1/A2/A3, WP-B1–B4 (G4 PARTIAL),
WP-C1 (composition + invalidation graph + hypothesis CAS + migration
032), WP-C2 (publication decision), WP-D2-pure (shadow action policy,
unwired), WP-D2-harness (G6-harness unit half: pure replay module +
fixtures, 11 tests), WP-D1-impl (typed call-edge record, dispatch
table + §5 authorization, mapping artifact, 67 tests, negative
matrix; `IAP_TYPED_EDGES` created, off). G5-formal PASS 2026-10-23:
live-DB interleave + role review executed via `scripts/g5_formal_driver.py`
against a fresh `g5_gate` database (alembic head 032, iap_test1
least-privilege case-runner) — 22/22 green, verdict JSON `/tmp/g5_verdict.json`
(operator-held). G6 ACCEPTED WITH OBSERVATIONS 2026-10-24 (owner
decision; not a clean PASS): shadow capture hook live on 20 dev-loop
runs (83 turns, 0 capture errors) with per-turn forced/owed stamping;
D/T 0.952 single set (INVESTIGATE trigger; sustained-HOLD needs a
second set), explained share 0.354, recovery agreement 0.778 with 0
reckless retries, no terminal turns observed, all runs abstained
(no verified PASS, no FAILED). Mechanism understood: shadow lacks
information-gain ordering (gathering-rank arbitrariness), loop
log_search/replan forcing is outside the shadow vocabulary, shadow
forcing is coarser than the loop's. Production launch still requires
the larger-sample gate (ledger OPEN item). D3-proceeding under these
observations: shadow stays advisory-only; per-family parity +
rollback required before any authoritative typed-edge use.
Open gates: G4 PARTIAL, G6-measure execution (larger sample;
operator), G7/G8 not started. No future-behavior flags exist in the tree beyond
the off-by-default `IAP_TYPED_EDGES` (`IAP_PROOF_KERNEL`,
`IAP_SHADOW_PLANNER`, `IAP_WITNESS_*` remain proposals, not code).

## 1. Dependency graph (remaining work)

```text
G5-unit ──┬──> D1-spec ──> D1-impl ──┬──> D3-family[definition] ──> D3-families ──> G7
          │                          │         (needs G6 too)                    (per-family)
          └──> G5-formal ────────────┘
                   (needs live-DB leg, operator)

D2-pure ──> G6-harness(unit) ──> G6-measure(live, operator) ──> G6 ──┘

G4-closure-track ──> G4 PASS ──> authoritative-rollout consideration
  (BEHAVIOR/ABSENCE/HISTORY rules; diverse reimplementation;
   A–T mapping; property/mutation/concurrency suites)

D-stable + native baseline ──> E1/E2 pilots (shadow, adoption bars) ──> G8
F1 continuous throughout. Stop conditions (plan §Gates) apply at every step.
```

Dependency rules:

- **D1-impl** requires G5-unit green + owner auth. D1-spec (design only,
  no behavior) may proceed any time.
- **D3** requires G5-formal + G6 + D1-impl (families migrate onto the
  typed-edge machinery), one family at a time in plan order
  (definition → call-site → runtime → absence/history), each with
  parity + rollback before the next starts.
- **G5-formal** requires the live-DB/RLS controlled interleave plus the
  migration/role review of the commit path — both operator-owned and
  unpassable in this environment.
- **G6** requires live-loop measurement (replay + divergence + budget +
  recovery on real runs) — operator-owned; the unit harness is buildable now.
- **E1/E2** stay deferred until D-interfaces are stable and a native
  baseline exists to compare against.
- **G4-closure** runs parallel to D-work and gates nothing in Stage D
  (D stays non-authoritative regardless), but gates any authoritative
  consideration.

## 2. Ordered steps

### Step 1 — G5-unit evidence (buildable now, no auth beyond standing rules)

Assemble the executable G5 gate minus the live leg: confirm invalidation,
conflict-denial, alternative-route, stale-publication-denial, unrelated-
preservation, and CAS-interleave tests are green (mostly present from
C1/C2); add mutation tests that must fail the suite (drop an
invalidation edge, weaken a scope check, bypass a staleness check).
Record the live-DB interleave and commit-path role review as explicit
OPEN operator items. Exit: G5-unit green + mutation-sensitive; G5
marked ASSEMBLED (formal pending live leg).

### Step 2 — D1-spec (design only, buildable now)

Specify the orthogonal call-edge record, the profile dispatch table,
and resolver soundness boundaries against the existing Java/Python
resolvers; define the `IAP_TYPED_EDGES` flag contract (off, never
authoritative before G5-formal). No code, no behavior change. Exit:
reviewed spec with exact schema + boundary tests enumerated.

### Step 3 — G6-harness, unit half (buildable now)

Replay harness over recorded histories through `decide()`: determinism
over fixture histories, divergence fixtures (shadow-vs-recorded
decisions), budget-accounting checks, recovery-priority checks, and a
static zero-side-effects proof (no I/O/clock/execution imports in the
policy module). Exit: harness green on fixtures; live measurement
recorded OPEN for the operator.

### Step 4 — G5-formal (operator-owned) — DONE 2026-10-23, verdict PASS

Controlled interleaving test against the real DB/RLS path (concurrent
premise change vs publication commit denies or reevaluates, never a
mixed snapshot) + migration/role review of the commit path. Exit: G5
PASS declared by the gate owner, or HOLD with named gaps.
Scope note 2026-10-24: the executed G5 protocol covered the hypothesis-save CAS path only; the protocol itself states no persisted publication record exists yet, so publication-commit atomicity remains open — see the G5 protocol §4 residual and docs/IAP-CurrentGaps-Fixes-impl-plan.md.

### Step 5 — D1-impl (requires Step 4 + owner auth) — DONE 2026-10-24, reviewed PASS (authorization-basis note 2026-10-24: the D1 spec sections 0/8-10 records D1-impl as not authorized pending explicit owner go-ahead plus entry conditions; that basis question is tracked in docs/IAP-CurrentGaps-Fixes-impl-plan.md and is not settled by this DONE line, which records build/verification state only)

Implement the Step-2 spec behind `IAP_TYPED_EDGES` (off): orthogonal
record, dispatch table, soundness boundaries, resolver goldens.
Authoritative use prohibited before G5-formal (already satisfied at
this point) and before per-family D3 rollout. Exit: flag-off
equivalence (identical outputs, rows, routing) + boundary tests green.

### Step 6 — G6-measure + D3-family[definition] (requires Steps 4–5 + G6)

Operator measures shadow-vs-live divergence, budget, and recovery on
real runs → G6 PASS. Then migrate the definition family: parity with
the native path + rollback + retry/pause/resume/budget tests. Exit:
definition family at parity with rollback proven; G6 PASS recorded.

Status 2026-10-24 (GO update): owner GO for D3 Phase 0
implementation and tests ONLY (Stage A closed: §9.2 audit
attached, §9.4 baseline 3058/4/6 exit 0, §6.2 probe plan;
package v1.5 governs — decision header cites v1.4 but its basis
exists only in v1.5). Boundary: shadow-only, both flags
default-off strict `"1"`, no Phase 1, no authoritative
consumption, no verdict/persistence changes beyond listed
additive keys, no migrations, no resolvers, no rollout;
authority flag incapable of activating a consumer, hard startup
refusal intact. Stage B (tests, differentials, ledger/baseline,
§9.0 suites) gates merge/acceptance; Stage C (live evidence +
separate decision) gates rollout/authority. Implementation
complete 2026-10-24 (88 new D3 unit tests green; full suite
3146 passed; flag-matrix identical both settings; integration,
contract, evaluation, benchmarks green or clean-skipped with
evidence-based exception for the live-stack system_api suite).
Stage B update 2026-10-24: CONDITIONAL GO for merge/acceptance
on reported evidence (owner decision). v1.6 is the as-built
clarification + acceptance record (v1.5 remains the
authorization basis). Conditions hold: system_api exception
explicit and bounded; dedup distinctions, T3 separate
attribution, shadow-only boundary, baseline and pre-existing
work protected. Merge itself still requires an explicit owner
instruction (not given); Stage C, Phase 1/authority, and
rollout remain unauthorized. Nothing committed.

### Step 7 — D3 remaining families, one at a time (each needs owner auth)

Call-site → runtime → absence/history, each gated on the previous
family's parity + rollback. Next family starts only after the current
passes. Exit per family: parity + rollback + budget evidence; final
exit: G7.

### Step 8 — E1/E2 pilots (deferred; requires D-stable + native baseline)

Analyzer/solver adapters under generic contracts, shadow-only, adoption
bars (incremental value + cost). Reject/defer by default per G8.
Exit: G8 PASS or documented deferral.

### Parallel track — G4 closure (any time, does not block Steps 1–7)

BEHAVIOR/ABSENCE/HISTORY rules with goldens; diverse validator
re-implementation (or equivalent independent oracle); explicit A–T
mapping; property/mutation/concurrency suites. Exit: G4 PASS, which
opens authoritative-rollout consideration — a separate decision, not
implied by any step above.

## 3. Standing rules for all steps

- One stage at a time; each needs explicit owner go-ahead (as C1/C2
  and D2-pure did).
- F1 (security/compat/release audit) rides every step.
- No new flags, schemas, or env switches without authorization; flags
  stay off until their gate passes; version-pinned routing only.
- Unit-provable work happens here; live-DB, live-battery, and
  production-path review are operator legs and stay OPEN until run.
