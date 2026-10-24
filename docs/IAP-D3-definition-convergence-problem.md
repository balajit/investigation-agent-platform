# Problem Definition 2/3 — D3-Definition Convergence or Focused Fix

> Branch context: `L2-D3-fix` (branched from `iap-onboarding-v0.1` @ `5a7eb13`).
> This prompt hands the D3-definition convergence problem to GPT for a first
> shot. Prerequisite: Problem 1/3 (`docs/IAP-L2-variance-diagnosis-problem.md`)
> must have named the L2 flip mechanism first — a fix built on an unnamed
> mechanism is not acceptable.

## 1. Background

The definition family did not reach parity: Stage C run set r2 verdict is
**INSUFFICIENT, kept open** (owner direction 2026-10-24, ledger line 501).
The r2 stop was triggered by the L2 native divergence
(shadow FAIL vs flag-off control PASS); the owner classified it as variance
and ruled the control arm sufficient (4 fresh flag-off runs + L3/L4
re-verified cached verdicts with recorded caveats).

Two lawful exits exist: (a) future runs naturally converge L2, or
(b) a focused fix for the named mechanism. This prompt covers exit (b).

## 2. Evidence pointers

- L2 diagnosis output (Problem 1/3 resolution — read it first).
- Freeze + run-set artifacts (frozen, do not modify):
  `docs/IAP-proof-kernel-d3-stageC-freeze-record-v2.md` (record hash
  `ff0cf8e7…`), manifest `docs/IAP-proof-kernel-d3-stageC-freeze-manifest-v2.json`,
  run set `artifacts/d3-stagec-2026-10-24-r2/` (freeze object
  `sha256:c1166139…`, ledger `sha256:1a8720e1…`, 6/48 reservations consumed).
- Shadow capture path (the certified surface any fix must not regress):
  `src/investigation_agent_platform/domain/proof/definition_shadow.py`
  (`_canonicalizable` float normalization, `pair_receipt`,
  `summarize_shadow_definitions` with `mirror_notes`),
  verify/conclude tap sites, receipt pairing, telemetry derivation/stamping
  in `application/worker/activities.py`, `ConcludeInvestigationInput` /
  conclude wiring in `application/worker/workflows.py`.
- Standing certification gate (ledger line 504, binding): any major change
  to the shadow capture path must re-pass the D3 shadow suite green
  (`tests/unit/test_d3_shadow_runtime.py` +
  `tests/unit/test_d3_shadow_bundle.py`, 49 tests) plus ruff on touched
  files; a live scratch-telemetry spot check (envelopes present,
  `capture_errors` 0) is required when runtime capture behavior changes.

## 3. Task for GPT

1. Take the named L2 mechanism from Problem 1/3 and design the minimal
   focused fix (narrowest behavior change that removes the flip; prefer
   deterministic grounding/retrieval/verification changes over prompt or
   model-behavior changes).
2. Show the fix does not disturb the certified capture path: float
   normalization (stored == digested), loud + counted pairing failures
   (no silent `except: pass`), T2/T3 mirror-note counting, N21 telemetry
   absence when the trace is empty, T3 conclude-gate pairing continuing
   the receipt chain.
3. Specify the convergence proof: which battery (fixtures, arms, repeats),
   what verdict pattern counts as converged (e.g. L2 PASS in N/N shadow
   runs + native parity on the flag-off control), and the exact gates to
   rerun (shadow suite 49, ruff, full unit suite noting the 2 pre-existing
   baseline-rev pins, live scratch spot check).
4. State freeze consequences explicitly: any `src/` change is material to
   freeze `ff0cf8e7…` and requires a new freeze record, new run-set id,
   and newly authorized budget before formal collection — so distinguish
   the *fix validation* runs (dev scope, scratch tenant) from any future
   *formal* collection.

## 4. Frozen constraints (do not violate in this prompt)

- This prompt authorizes design + specification + test plan only.
  Implementation needs a separate explicit owner go-ahead.
- No changes to frozen artifacts (`artifacts/d3-stagec-2026-10-24-r2/`,
  freeze record v2, manifest v2).
- No commits without explicit owner approval.

## 5. Acceptance criteria

- [ ] Fix sketch traces to the named mechanism (no mechanism, no fix).
- [ ] Capture-path non-regression argument for each certified behavior.
- [ ] Convergence proof specified (battery shape, verdict bar, gate list).
- [ ] Freeze consequences stated (dev validation vs formal collection).

## 6. Non-goals

- No implementation, no live runs, no freeze/re-freeze actions.
- No D3-family rollout beyond definition; no authority/production claims.
