# Problem Definition 3/3 — D3 Remaining Families Rollout

> Branch context: `L2-D3-fix` (branched from `iap-onboarding-v0.1` @ `5a7eb13`).
> This prompt hands the D3 remaining-families rollout to GPT for a first shot.
> Prerequisites: Problem 1/3 (mechanism named) and Problem 2/3 (definition
> family converged or focused-fix certified). Families roll out strictly in
> order — never in parallel, never skipping.

## 1. Background

Per the forward plan (`docs/IAP-proof-kernel-forward-plan-v1.0.md` Step 7)
and the kernel plan (`docs/IAP-proof-kernel-plan-v1.0.md` WP-D3), the D3
family migration order is definition → call-site → runtime →
absence/history, each gated on the previous family's parity + rollback, with
final exit G7 (per-family parity + rollback + retry/pause/resume/budget
tests). Definition is the current family (r2 INSUFFICIENT, kept open;
L2 variance under diagnosis). The remaining families are: **call-site**,
**runtime**, **absence/history**.

Each family migration requires: shadow-vs-native parity with the native
path, rollback proven, retry/pause/resume/budget behavior, and the standing
certification gate (D3 shadow suite green + ruff; live scratch-telemetry
spot check for runtime capture changes) — per ledger line 504.

## 2. Evidence pointers

- Problems 1/3 and 2/3 resolutions (read both first).
- D3 design + acceptance basis: `docs/IAP-proof-kernel-d3-definition-plan-v1.3.md`,
  approval package `docs/IAP-proof-kernel-d3-phase0-approval-package-v1.9.md` + Amendment A1,
  harness proposal `docs/IAP-proof-kernel-d3-stageC-harness-design-proposal-v1.15.md`,
  build authorization `docs/IAP-proof-kernel-d3-stageC-harness-build-authorization-request-v1.0.md`.
- Prior run sets as method precedent (frozen, do not modify):
  `artifacts/d3-stagec-2026-10-24-r1/` (aborted on INVALID_FLOAT),
  `artifacts/d3-stagec-2026-10-24-r2/` (stopped per §6.2g, verdict INSUFFICIENT).
- Battery fixtures: `tests/fixtures/battery_cases/` (L1–L12; live vs offline-only).
- Forward-plan dependency rules (`docs/IAP-proof-kernel-forward-plan-v1.0.md`
  §1): G4-closure runs parallel and gates nothing in Stage D; G6-measure
  larger-sample gate is a separate operator leg; E1/E2 stay deferred.

## 3. Task for GPT

1. For the next family in order (call-site first), define the family scope:
   which claim kinds / code paths it covers, what "parity with the native
   path" means measurably for that family, and what rollback proven looks
   like (flag-off equivalence procedure).
2. Specify the collection shape per family: fixtures (from the battery set;
   justify inclusions/exclusions), shadow + control arms, reservation
   budget shape (cells, caps — mirror the r1/r2 12-per-bucket / 48-total
   pattern or justify a deviation), stop rules (native divergence handling
   per the r2 L2 precedent: stop, record, owner classifies), and the
   floor/checklist the family must meet.
3. Specify the freeze/record pattern per family (new freeze record, new
   run-set id, fresh budget — following the v1→v2 precedent), including
   what carries over unchanged (protocol v1.9+A1, registry, vectors)
   and what is family-specific.
4. Sequence the three families with entry/exit gates: no family starts
   until the previous passes; state each family's exit evidence precisely.
   End state: G7 with per-family evidence packets.

## 4. Frozen constraints (do not violate in this prompt)

- Design + specification + sequencing only. Each family still needs its
  own explicit owner go-ahead before any implementation or collection.
- No changes to frozen artifacts (r1/r2 run sets, freeze records, manifests).
- No commits without explicit owner approval. No `tmp/` references.

## 5. Acceptance criteria

- [ ] Per-family scope, parity definition, and rollback procedure specified.
- [ ] Per-family collection shape (fixtures, arms, budget, stop rules).
- [ ] Freeze/record pattern per family (carry-overs vs family-specifics).
- [ ] Ordered gate sequence ending at G7, each gate with exit evidence.

## 6. Non-goals

- No implementation, no live runs, no freeze actions, no G7 declaration.
- No authority/production/rollout claims (G4 PASS + separate decision required).
- No E1/E2 pilot work (deferred until D-stable + native baseline).
