# Proof-Kernel Stages B/C/D Closeout Plan

## Context

Stage A (baseline and diagnostics) is complete: frozen baseline dataset with a committable-green freezer, the 7-bucket attribution vocabulary live in battery reports, stored-vs-derived ledger linkage, and success/empty/adversarial case coverage. Its go/no-go (reproducible baseline) is met.

This plan closes every remaining item in Stages B, C, and D identified across the review track (`tmp/` GPT research reviews, the independent review verdict, the G4 disposition package with C1–C4 residuals, and the recommendations ledger), ordered by the dependency graph. Stage E (analyzer/solver witnesses) stays deferred by design and is out of scope.

Standing conventions apply throughout: version-pinned behavior (never env-switched), no authoritative rollout without explicit owner authorization, shadow-before-authoritative for any new proof path, and battery parity gates on every behavior change.

## Dependency graph

```mermaid
graph TD
    W0A[W0.1 C3-race check] --> W1
    W0B[W0.2 D3-merge state] --> W1
    W1B4[B4 traceability sign-off<br/>OWNER DECISION] --> B1
    W1B3d[B3 mapping-authority design<br/>(done, PROVISIONAL — deferred)] --> B3i[B3 implementation<br/>(deferred to reopen trigger)]
    W1C2[C2 pure-test gap-fill] --> C2i[C2 live-path integration]
    W1D3[D3 G6 baseline sample<br/>LIVE] --> TERM[Terminal launch gate]
    B1[B1 G4 residuals + re-disposition<br/>OWNER DECISION] --> B2
    D3T[Track A retest on D3 fixes<br/>already owned] --> B1
    B2[B2 ARCH-CRIT-001 kernel wiring] --> C1
    B2 --> D1
    C1[C1 atomic publication wiring] --> C2i
    C1 --> D1
    C2i --> D1
    B3i --> D4G7[D4/G7 ops + attestation]
    D1[D1 general native-edge premises] --> D2[D2 native router shadow]
    C2i --> D2
    D2 --> TERM
    B4 --> TERM
    D4G7 --> TERM
```

Reading: an edge A → B means B needs A's output. Items with no incoming edge can start immediately. Owner-decision nodes gate everything downstream of them.

## Wave 0 — Verify-only (no authorization needed)

Ground truth before committing to work. Either closes items outright or converts them into scoped Wave 1/2 work.

### W0.1 — C3-race disposition check [ANALYSIS, half day]

Ledger item 431 calls for one serialized §9.3 battery re-run after a Stage B/C parallel race. Since then the D3 line merged, hybrid-line Track A certified offline gates, and full-suite baselines moved. Determine whether the race condition the item describes can still exist (same tree raced twice) or is superseded.
- Acceptance: item closed as superseded with lineage evidence, or converted into a single specified re-run with exact tree + command. No code.

### W0.2 — D3-merge and retest-trigger state [ANALYSIS, half day]

Confirm which D3 families are merged, which fixes are pending retest under the Track A procedure, and whether the procedure's trigger conditions are currently armed. This sets the input queue for B1 re-disposition.
- Acceptance: a dated list of merged vs pending D3 work. No code.

## Wave 1 — No owner GO required (standing rules apply)

### B4 — Traceability sign-off package [DOCS + OWNER-DECISION]

The matrix is promoted to `docs/IAP-proof-kernel-traceability-v1.0.md` but counts as G4 A–T evidence only after owner sign-off (ledger item 571). Assemble the sign-off package: gate tallies reconciled to the current tree, K-status deltas since promotion, and explicit residuals list. No code, no behavior change.
- Depends on: nothing (can start now; rebase tallies at signing time).
- Acceptance: owner signature recorded in the disposition package; ledger flips to IMPLEMENTED.

### B3-design — Real-world mapping authority [DESIGN — DEFERRED 2026-10-28]

Component-2 BEHAVIOR discharge works only on synthetic vectors (`provenance_ref` + `mapping_digest` admitted as test-only attestation). The design exists (`docs/IAP-proof-kernel-mapping-authority-design-v1.0.md`) but is marked **provisional** per owner decision: no named consumer or trust boundary requires independently verifiable build mapping, and IAP's revision-aware evidence linking with honest abstention already covers the investigation need without signatures. Reopen triggers (either suffices): a named consumer (e.g., customer audit exports must be tamper-evident) or a specific threat model naming a distrusted mapping provider. Until then, `BUILD_SOURCE_MAPPING` stays unsupported for real-world use (synthetic vectors only) — the honest recorded gate state.
- Depends on: a reopen trigger (above), then owner GO for B3 implementation.
- Acceptance: design doc with contract + threat model + migration path (done, provisional); no code unless reopened.

### C2-tests — Composition/invalidation/replay gap-fill [CODE + TEST]

Pure machinery exists (`compose_conjunction`/`compose_alternatives`/`invalidate_derivations`/cycle detection) with unit tests, plus replay-harness and composition-gate tests. Close the pure-layer gaps the go/no-go names: concurrent-evidence-update tests, replay-against-captured-trace tests, and mutation tests proving weakened scope / missing invalidation checks are caught (C3's M1–M6 are detected; extend to composition-level mutations).
- Depends on: nothing (pure layer, no production contact).
- Acceptance: new tests green; no production behavior change.

### D3-run — G6 baseline sample execution [LIVE]

The frozen G6-measure protocol needs a baseline sample at the adopted floor (≥20 eligible runs spanning PASS+FAIL, zero capture errors). Run it against the current tree with no code changes, record divergence/explained-share/recovery exactly as specified. Live spend requires the standard live-run GO; the run itself changes nothing.
- Depends on: W0.2 (stable tree window — no merges mid-sample).
- Acceptance: a run-set report in `reports/` with all four bars computed; qualifies as the pre-change baseline for every later gate.

## Wave 2 — Owner GO required

### B1 — G4 residuals closure + re-disposition [DESIGN + CODE + TEST + OWNER-DECISION]

Close the enumerated residuals, then return for re-disposition:
- K13 to PASS (BEHAVIOR/ABSENCE/HISTORY rule completion per the K-matrix).
- C2-R2 producer tightening (separate C2 amendment decision — CALL_EDGE producer/checker divergence).
- A/C/F mapping or signed rationale carry-forward.
- C3 scope-visibility gap (definition/call_edge checker-K11-only sensitivity).
- Diverse validator re-implementation (the large item: a second decision path so the checker does not share the pure validators — removes the correlated-defect risk the gate explicitly names).
- C2-R1 real-world authority is B3's track; C2-R3/C2-R4 and C1-R1 ride along as documented.
- Depends on: Track A retest on any landed D3 fixes (already owned); B4 sign-off informs but does not block.
- Acceptance: full K-matrix green-or-signed-off, 14-file gate green, owner re-disposition recorded (PASS scoped or PARTIAL with new direction).

### B2 — ARCH-CRIT-001 kernel wiring [DESIGN + CODE + CONFIG + LIVE + OWNER-DECISION]

Route the conclusion path through the kernel: where conclude calls `certify()` → `check_certificate()` → `decide_publication()` → commit, with fail-closed semantics when the kernel is unavailable, and flag-gated rollout (shadow/record-only first, authoritative only on explicit authorization). `certify`, the checker, and `publication_commit_service` exist with no production callers — this item is the wiring plus the rollout, not new kernel logic.
- Depends on: B1 direction (G4 must directionally approve authoritative use); owner rollout authorization.
- Acceptance: design with wiring diagram + fail-closed matrix; code behind flags; flag-off equivalence proven (differential identical); live parity on the shadow arm; explicit authorization before any flag-on.

### C1 — Atomic publication end-to-end [DESIGN + CODE + CONFIG + LIVE + OWNER-DECISION]

The G5 atomicity proposal (v1.0) exists; G5 PASS is declared and committed. What remains excluded by prior decisions: writer routing (`single`/`all`/`any`), production wiring of the commit service, migration decisions for the publication tables, and the live interleave proof. Implement the proposal's D1–D4, decide writer routing, wire conclude → commit service in a single transaction/CAS binding context + proof inputs + publication records.
- Depends on: B2 (authoritative kernel must exist to commit); owner decisions on implementation GO, migration, and writer routing.
- Acceptance: end-to-end atomic publication proven by live interleave tests under least-privilege roles; no mixed-snapshot publication possible.

### B3-impl + G7 ops [DEFERRED with B3-design — see above]

Implement the mapping-authority design; stand up the CI/registry attestation; G7 operations-side acceptance.
- Depends on: B3 reopen trigger, then owner GO.

## Wave 3 — Native track (needs B+C live)

### D1 — General native-edge premises [DESIGN + CODE + TEST + LIVE]

The kernel has a `call_edge/v1` rule, `DispatchRow` digests, and the `typed_edges_enabled` gate, but consumption is D3-shadow-scoped. Generalize typed-edge premises across claim kinds with the kernel consuming them as premises (not advisory labels), under the same shadow-first rule as B2.
- Depends on: B2 + C1 live (authoritative kernel + atomic publication to consume edges safely).
- Acceptance: parity battery with zero new false accepts on adversarial fixtures; flag-off equivalence.

### D2 — Native router with shadow comparison [DESIGN + CODE + LIVE]

One authoritative executor; the deterministic policy runs in shadow on captured inputs (never hidden live reads on replay), with recovery, duplication, and budget-overrun bars per the stage go/no-go.
- Depends on: D1, C2 live integration.
- Acceptance: correct recovery on target cases, zero new false accepts, no flag-off behavior changes, no duplicated actions or budget overruns.

### Terminal production launch gate [OWNER-DECISION]

Requires, jointly: G4 re-disposition to scoped PASS (B1), B2 + C1 live and authorized, D3 sample evaluated against bars at the adopted floor (no sustained HOLD), B4 signed, G7 accepted. No single item substitutes for the conjunction. Launch itself is a separate explicit GO, never implied by gate passage.

## What this plan explicitly does not include

- Stage E (analyzer/solver witnesses): deferred by design; each adapter needs its own graduation decision after D-stable.
- The stale-golden and tenant-flake follow-ups already in the ledger: test hygiene, not proof-kernel scope.
- Any change to the standing rule that authoritative rollout needs explicit owner authorization per step.
