# B1 Residuals Closure Note — 2026-10-28 (Wave 2, part 1: no-decision items)

Scope: B1 items from `docs/IAP-proof-kernel-bcd-closeout-plan.md` §Wave 2 that need no
owner design decision. Decision-gated items (C2 amendment, obligation-layer design,
diverse-validator scope, B1 re-disposition) are recorded here as pending, not closed.

## Closed in this batch

### C2-R3 — premises suite list (CLOSED)
Rode with the C3 supplement per the disposition package. The gate command in
`docs/IAP-proof-kernel-traceability-v1.0.md` includes
`tests/unit/test_proof_kernel_premises.py`; the signing rebase (2026-10-28) ran it
green (25 collected/passed) alongside the 13-file gate. No further action.

### C2-R4 — baseline pins + untouched-file mypy noise (CLOSED with carry)
The two pre-existing baseline revision-pin failures were resolved by the git-rev
provenance fix (recorded, excluded from equality; frozen baseline regenerates
deterministically) — verified in the signing rebase (kernel baseline 5/5 green).
Untouched-file mypy noise remains out of scope per disposition. No further action.

### C1-R1 — fixture placeholder digests (CARRIED, structural-only)
`content_hash` values remain symbolic by fixture-envelope design; digest-to-byte
binding proven by C2 vectors; no gate relies on C1 placeholder digests. Unchanged;
carried as documented. No action.

### A/C rationale carry-forward (CLOSED by sign-off)
A (speculative laundering) and C (repair across boundary) NOT MAPPED rationales in
the traceability matrix were accepted as G4 A–T evidence by the 2026-10-28 owner
sign-off (`docs/IAP-proof-kernel-g4-signoff-package-v1.0.md`). The sign-off is the
signed rationale carry-forward the plan calls for. No further action unless a
kernel-adjacent gap is demonstrated (A) or cross-boundary repair is claimed as
kernel evidence (C).

### F wrong-witness (CARRIED to obligation follow-up)
Explicit F test requires obligation objects, which do not exist (see K13-B1 below).
Carried, not closable in B1. No action.

### R-C3-01 — M2 mapping re-point (CLOSED)
The M2 "Expected detector(s)" row in
`tests/unit/test_proof_kernel_g4_component3_properties.py` now names the
direct-semantics K11 detectors instead of the end-to-end names that cannot fire
(redundant inline K11 at `certificate.py:588-600` catches the mismatch first —
recorded defense-in-depth, retained). Three executable direct detectors added:
`test_k11_direct_definition/call_edge/behavior_revision_mismatch` in
`tests/unit/test_proof_kernel_g4_component4_integration.py` — each asserts VALID
on the true cert, then SCOPE_MISMATCH with a K11 reason on a cert whose scope
revision is swapped, calling the `check_*_semantics` function directly and
bypassing both `check_certificate` inline layers. 23/23 green in the C4 module.

### K13-A1 — exhaustive decision-path denial (CLOSED)
`test_non_valid_checker_denies_k13` now covers all 9 non-VALID `CheckerOutcome`
members (was 4). `decide_publication` denies each with NOT_CHECKER_ACCEPTED.
No source change (the gate already denied all non-VALID); the test now proves it.

### K13-A2 — CAUSAL abstention end-to-end pin (CLOSED)
New `test_causal_abstention_denies_end_to_end_k13` (in
`tests/unit/test_certificate_checker.py`): `rule_for_kind(CAUSAL)` is None,
`get_rule("causal/v1")` raises `UnknownRule`, a forged causal/v1 cert checks to
UNSUPPORTED_CONSTRUCT, and feeding that outcome to `decide_publication` denies
with NOT_CHECKER_ACCEPTED. Abstention can never route around the registry into
a COMMIT through the real path.

## Pending owner decisions (NOT closed by this batch)

1. **C2 amendment** (C2-R2 producer tightening ± producer revision sensitivity for
   definition/call_edge, bundled as one supplement per the §4.1/§4.2 analysis):
   Option A (tighten to checker parity, recommended), B (document-and-hold),
   C (duplicates only). Contract change to frozen rules + fingerprint re-record;
   cannot proceed without the amendment decision.
2. **Obligation-layer design** (K13-B1/B2): closed obligation model +
   `expand_obligations` kind table + status vocabulary, then the `decide_publication`
   gate extension. New kernel contract; needs owner design approval (WP-C1 per matrix).
   Until then K13 stays PARTIAL (decision-path half now exhaustively pinned; promotion
   half has no objects to gate on).
3. **Diverse-validator scope** (Option 1 full second path with conjunction vs Option 2
   targeted shared-core re-implementation with shadow-audit first). Recommendation:
   Option 2 now (no wiring change, no availability risk), Option 1 per-rule promotion
   after differential suites go green.
4. **Legacy-path closure + authoritative wiring** (K13-B3): writer/caller audit +
   G5 atomicity — G5-owned, needs migration/writer-routing decisions and live env.
5. **B1 re-disposition**: after the above resolve, owner records scoped PASS or PARTIAL
   with new direction. G4 stays PARTIAL regardless until D3-family retesting completes.

## Verification (this batch)
- `test_certificate_checker.py` + `test_publication_decision.py`: 39 passed.
- `test_proof_kernel_g4_component4_integration.py`: 23 passed (20 + 3 new).
- Fingerprints: unaffected (no `src/` change in this batch).

## Re-disposition (owner decision 2026-10-28, Wave-2 session)

**B1: PARTIAL with new direction. G4: stays PARTIAL.**

- Scoped PASS on the completed B1 items: K13 decision-path pins (A1/A2),
  R-C3-01 re-point with executable detectors, doc-level closures
  (C2-R3, C2-R4 pins, C1-R1 carry, A/C rationale carry-forward via
  sign-off), C2 amendment Option A (producer parity + revision join,
  bundled), diverse validator Option 1 (second path + conjunction +
  21-test differential suite).
- NOT closed by B1: K13 promotion half (no obligation objects; obligation
  design v1.1 in owner review, implementation needs separate GO), legacy
  writer closure (K13-B3, rides B2/C1 rollout decisions), real-world
  mapping authority (C2-R1, B3-deferred), D3-family retesting (pending,
  owner-triggered).
- A B1 PASS would overstate closure against the closeout plan's K13
  requirement; PARTIAL records the completed work without claiming the
  promotion half.
- Direction: owner reviews obligation design v1.1 (R2); B2/C1 proceed to
  design-only work only on separate authorization (R3/R4 gates defined,
  work not started).
