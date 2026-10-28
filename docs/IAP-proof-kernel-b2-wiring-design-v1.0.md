# B2 Kernel-Wiring Design Proposal v1.0 (DRAFT — for owner review, no code)

Design-only. No flags are added, no call sites are wired, no live runs
are authorized by this document. Each implementation step needs its own
explicit GO (R3 gates below).

## Current state (verified against the tree)

- `conclude_investigation_activity`
  (`application/worker/activities/lifecycle.py:2051`) has NO kernel
  contact: no import of `certify`, `check_certificate`,
  `decide_guarded_promotion`, or any `domain/proof` symbol anywhere in
  `application/` except the test-only
  `application/proof/publication_commit_service.py` (itself callerless).
  Conclusions persist via ConclusionGate + lifecycle projection today.
- The kernel promotion entry is now `decide_guarded_promotion(policy,
  evidence_bytes, trusted_context_bytes)` (B1, v1.3 contract): pure,
  synchronous, deterministic; denial is closed and reasoned.
- Synchronization boundary: the activity is async and DB-backed; the
  kernel is pure/computational. The wiring therefore assembles inputs,
  calls the kernel synchronously, and acts on the verdict — no kernel
  code ever touches the session, and no DB state ever enters the kernel
  except as content digests inside evidence bytes.

## What gets wired (proposal)

For each investigation reaching conclusion with candidate findings:

1. **Claim construction.** Map each concluded finding to a kernel claim
   (kind + proposition) derived from the verifier's banked evidence
   (cited rows, revisions, scopes). Findings without checkable claims
   (`NOT_CHECKABLE`) skip the kernel path explicitly — recorded as
   `OBLIGATIONS_ABSENT`-class skip, never silently promoted.
2. **Evidence assembly.** Build the per-claim certificate via the
   existing certify path (pure), plus manifest + artifacts from the
   banked evidence store. Canonical bytes via the obligation helpers.
3. **Trusted context.** Bind the tenant's `DecisionPolicy` (operator
   config; B2 wiring owns tenant→policy selection), repository bindings
   from the provider/profile boundary, and the current publication
   context (expected context, reconciliation, eligibility).
4. **Verdict application (flag-gated, §Flags).** In shadow mode the
   verdict is logged alongside the conclusion and changes nothing. In
   enforcing mode (separate GO), a DENY blocks conclusion persistence
   for the affected findings (recorded, retryable); COMMIT allows the
   existing persistence path unchanged.

Out of scope for wiring: claim-kind coverage beyond what the verifier
banks (no new retrieval); obligation persistence (G5); mapping
authority beyond the test-only set (B3-deferred).

## Flag design (no flags created by this doc)

- `IAP_KERNEL_WIRING=off` (default): zero kernel contact; byte-identical
  behavior to today. Proves flag-off equivalence by construction.
- `IAP_KERNEL_WIRING=shadow`: run steps 1–3 plus the guarded entry per
  claim; log `(derivation_id, decision, reason, obligation statuses)`;
  never gate persistence. Differential bar for promotion to enforcing:
  N live-equivalent runs with zero verdict/persistence divergence
  attributable to wiring (divergences fail closed to analysis, never
  to silent promotion).
- `IAP_KERNEL_WIRING=enforcing`: DENY blocks persistence (separate GO;
  availability trade-off is explicit — kernel failure denies
  conclusions rather than passing them through).

Flag evaluation is process-startup, never per-request; an unknown value
fails closed to `off` with a startup error, not a warning.

## Fail-closed matrix

| Failure | Shadow | Enforcing (proposed) |
|---|---|---|
| Kernel import/eval exception | log + continue unguarded | deny conclusion persistence, recorded |
| Policy missing / no repository match | log `POLICY_DENIED` | deny |
| Evidence assembly failure (no cert) | log `OBLIGATIONS_ABSENT` | deny |
| Checker non-VALID | log checker reason | deny |
| Obligation OPEN/UNSUPPORTED | log obligation reason | deny |
| Context moved between assembly and verdict | re-assemble once; persistent move denies `STALE_OBLIGATION`/`STALE_CONTEXT` | same |

No row ever promotes on kernel failure. Shadow mode never blocks.

## Explicit GOs required (R3 decomposition)

- **R3a (this doc):** accept/amend the wiring design.
- **R3b:** implement shadow arm only (assembly + logging; no gating;
  flag defaults `off`). Requires live-parity GO for the shadow sample.
- **R3c:** enforcing flag-on authorization (separate; needs R3b parity
  evidence + availability sign-off).
- B2 does not migrate data, write tables, or route writers (C1 scope).

## Verification (for the implementation GOs, not this proposal)

- Flag-off equivalence: conclusion records byte-identical with flag off
  across the battery corpus.
- Shadow differential: verdict log vs conclusion outcomes; every DENY
  manually adjudicated once (tuning signal, not auto-promotion).
- API scan: only the conclude path + service reference the guarded
  entry outside `domain/proof` and tests.
