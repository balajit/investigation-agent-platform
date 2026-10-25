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

### Dev verifier consolidation

The active development runtime now has one verifier behavior. Historical
`v1`/`v2`/`v3` labels are retained only for audit and frozen-artifact
compatibility and do not select different runtime semantics. New development
investigations do not create verifier-version pins. This does not modify the
signed r2 freeze or its formal eligibility rule: the existing D3 protocol is
explicitly `verifier_version == v2`, so any post-consolidation formal D3
collection requires a new owner-authorized freeze and run-set decision.

### Eligibility mapping (unified behavior vs frozen v2 rule)

- Frozen r2 rule: eligible turn = recorded native outcome + recorded typed
  observation under the frozen comparison rule, `verifier_version == v2`,
  on a definition-claim candidate (approval package v1.9 §§0.2/2.1, A-1).
- Current dev: `resolve_verifier_version` returns the single active behavior
  for all audit-compatible labels (`v1`/`v2`/`v3` → `("v2", "unified-dev")`;
  unmarked → `("v2", "unmarked")`), unknown labels still fail closed, and
  `VerificationEngine(verifier_version=...)` no longer branches on the
  argument. Historical rows keep their recorded labels for audit only.
- Consequence: unified dev behavior cannot claim frozen v2 eligibility. Any
  post-consolidation formal collection must be governed by a newly authorized
  protocol amendment that redefines eligibility (e.g. active-behavior version
  + protocol version recorded separately), states how pre-consolidation rows
  are treated (excluded or re-baselined, never silently re-interpreted), and
  updates observation tagging, join keys, and telemetry labels accordingly.
  Until that amendment plus a new freeze/run-set/budget exists, all runs on
  this tree are dev validation (scratch tenant), never formal collection.

## 4. Focused fix design (L2 caller-enumeration binding)

Mechanism (from Problem 1/3): the reasoner may emit one aggregate caller
hypothesis (e.g. `OrderQualifyHelper and OrderServiceRestProxy call
OrderServiceHandler.handle`) while persistence only records the claims the
model emitted. The verifier deterministically requires every named caller to
have its own bound `CALL_EDGE` edge claim with a range read showing the
invocation (`verification.py:1987-2066`; `call_site_proof` at
`verification.py:1838-1891`). The workflow repair seam forced `GET_CODE` for
unread caller files but did not repair a missing per-caller claim or bind the
range to a new edge assessment. Result: formulation/binding gap with
verification demotion as the deterministic consequence; r2 stays
`DIVERGE-STOP`/`INSUFFICIENT` and is not reinterpreted.

Fix (narrowest behavior change, native path only; D3 stays observational):

- `DecisionClaim` accepts structured `caller_edges`; reasoner sanitizer and
  prompt require one target and evidence binding per edge.
- Persistence expands aggregate caller edges, records one typed assessment per
  edge, stores unresolved obligations with `evidence_id=None` and stable reason
  codes (`CALL_EDGE_UNRESOLVED`/`NO_CALLSITE`/`REVISION_MISMATCH`), and stores
  required edge keys in the existing JSON `required_verification` field as
  `CALL_EDGE|<key>`. No migration or new table.
- Canonical obligation key: `symbol|counterpart|normalized file_path|commit_id`
  via a single shared helper with `|`-escaping and identical normalization on
  both the workflow forcing side and the verification coverage side, so forced
  reads and coverage checks join on the same identity.
- Workflow call-site forcing carries `caller_edge_key` for structured edge
  targets before the existing definition-read forcing; one unresolved
  obligation forces one bounded `GET_CODE`, and the returned row binds to that
  obligation's assessment (capped, typed reason when it cannot complete).
- Verification requires every persisted caller obligation to be proven by
  invocation evidence: each edge target passes `call_site_proof` (same-file
  sibling range reads may repair a cited search/comment row but must still
  prove the specific edge), named callers from file stems must be a subset of
  proven callers (class stem as well as method fragment counts), and
  `required - proven_keys` must be empty (`CALL_EDGE_INCOMPLETE_ENUMERATION`
  names the missing edge). `ConclusionGate` consumes the same coverage via
  `caller_edges_validated`, allowing a structural caller answer without a
  causal chain while staying fail-closed on incomplete coverage.
- Shadow taps, receipt pairing, and flag behavior are unchanged.

Capture-path non-regression argument (each certified behavior):

- Float normalization (`stored == digested` via `_canonicalizable`): untouched;
  caller-edge code passes no floats into canonical envelope bytes.
- Loud + counted pairing failures (no silent `except: pass`): untouched pairing
  block; new path-match code narrows exceptions and never swallows envelope
  pairing. New `CALL_EDGE_*` reason codes are verification outcomes, not
  capture errors, and do not increment `capture_errors`.
- T2/T3 mirror-note counting: summarizer separation of `mirror_notes` from
  `capture_errors` preserved; no new mirror sites added.
- N21 telemetry absence when the trace is empty: no new telemetry keys added by
  this fix; empty trace still stamps no D3 keys.
- T3 conclude-gate pairing continuing the receipt chain: conclude path threads
  `caller_edges_validated` through the existing gate decision only; receipt
  start/iteration wiring unchanged.

Convergence proof specification (to be executed only under new authorization):

- Battery: L2-focused shadow runs on the frozen L1–L12 fixture set (L2 primary;
  L1/L4 as regression guards), repeated identical flag-on executions plus one
  fresh flag-off control on the same fixtures. Scratch tenant only until the
  new freeze names the formal tenant.
- Verdict bar: no run publishes an incomplete enumeration; observed PASS/FAIL
  sequence is stable across the pre-authorized repeat budget with a binomial
  pass-rate confidence interval (one PASS is not convergence). Flag-off control
  must show native parity with its shadow counterpart and D3-key absence; a
  fresh control divergence stops the probe per the §6.2g precedent.
- Per-run record: verdict + conclusion status; caller obligations/proven/
  unresolved counts; per-obligation evidence IDs, file/revision/range, gate
  reason; retrieval vs repair turns and forced call-site reads; verifier and
  conclude-gate blockers; shadow capture health + native/control parity.
- Gates to re-pass: D3 shadow suite green
  (`test_d3_shadow_runtime.py` + `test_d3_shadow_bundle.py`, 49 tests), ruff on
  touched files, full unit suite green except the 2 pre-existing baseline-rev
  pins (currently blocked during collection by missing secret-store modules in
  `test_datasources.py`/`test_secret_stores.py` — restore or explicitly carve
  out before freeze), live scratch-telemetry spot check when runtime capture
  behavior changes (envelopes present, `capture_errors` 0).

Freeze consequences:

- Any `src/` change on this branch is material to freeze `ff0cf8e7…` and to run
  set `d3-stagec-2026-10-24-r2`. No formal collection may proceed under the old
  freeze, old run-set id, or old budget.
- Fix validation runs (dev scope, scratch tenant, existing scripts only) are
  permitted as engineering signal; they are not formal collection and create
  no verdict, parity, or rollout claim.
- Formal collection requires, in order: owner-authorized protocol amendment
  (eligibility mapping above) → new freeze record → new run-set id → newly
  authorized budget (retries consume the frozen budget; voided sets are
  terminal per §6.2e).

## 5. Acceptance criteria

- [ ] Fix sketch traces to the named mechanism (no mechanism, no fix).
- [ ] Capture-path non-regression argument for each certified behavior.
- [ ] Convergence proof specified (battery shape, verdict bar, gate list).
- [ ] Freeze consequences stated (dev validation vs formal collection).

## 6. Non-goals

- No implementation, no live runs, no freeze/re-freeze actions.
- No D3-family rollout beyond definition; no authority/production claims.
