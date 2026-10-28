# G4 Traceability Sign-Off Package v1.0 (DRAFT — awaiting owner signature)

## Purpose and scope

This package asks the owner for one thing: signature confirming that
`docs/IAP-proof-kernel-traceability-v1.0.md` (with the reconciliations in
§2 below) constitutes the G4 A–T explicit-mapping evidence. It is not a
re-disposition: G4 remains PARTIAL with direction per the 2026-10-26 owner
decision until B1 residuals close and D3-family retesting completes. Signing
here changes no gate status; it records agreement on the evidence basis.

## Reconciled tallies (current tree)

Re-ran 2026-10-28 on the closeout branch (current tree). Full unit suite:
3992 passed / 4 skipped / 6 xfailed, with only the 2 known pre-existing
drift pins (`test_phase0_contract_repair`, `test_phase2_remaining`).

| Suite | Promoted count | Current | Delta |
|---|---|---|---|
| WP-A2 diagnostics | 9 | 9 passed | — |
| WP-A3 retrieval ledger | 18 | 18 passed | — |
| Version containment | 14 | 14 passed | — |
| WP-B1 canon identity | 20 | 20 passed | — |
| WP-B2 manifest evaluation | 14 | 14 passed | — |
| WP-B3 typed rules | 11 | 11 passed | — |
| WP-B4 certificate checker | 27 | 27 passed | — |
| G4 premises | 25 | 25 passed | — |
| G4 component 2 rules | 76 | 76 passed | — |
| G4 component 3 properties | 39 | 39 passed | — |
| G4 validator fingerprints | 4 | 4 passed | — |
| G4 component 1 evidence | 5 | 5 passed | — |
| Onboarding CLI | 15 collected | 15 collected (1 skipped) | — |
| WP-C1 composition/invalidation | 18 | 18 passed | — |
| WP-C1 hypothesis CAS | 10 | 10 passed | — |
| WP-C2 publication decision | 11 | 11 passed | — |
| G5 composition gate | 7 | 7 passed | — |
| Kernel baseline | 5 (2 rev-pin failures) | 5 passed | **Pins resolved** — `git_rev` is now recorded provenance, excluded from equality (committed); frozen baseline regenerates deterministically |
| Fixture integrity | 14 | 14 passed | — |
| G6-harness canonical + shadow policy | 25 + 15 | 25 + 15 passed | — |
| D3 shadow runtime | 29 | 30 passed | **+1 test** (growth, green) |
| D3 shadow bundle | 20 | 20 passed | — |
| G4 component 4 integration | 20 | 20 passed | — |

Kernel suites total: 432 collected, 431 passed, 1 skipped (pre-existing
onboarding skip), 0 failed.

## K-status deltas since promotion

- **K13 PARTIAL — unchanged.** Obligation-gated promotion still has no
  obligation objects; legacy authoritative paths remain outside the
  kernel's enforcement boundary. No new bypasses introduced (kernel
  modules remain pure and unwired; verified by import audit
  2026-10-28 — no production callers of `certify`/`check_certificate`
  outside `publication_commit_service`, which itself has no callers).
- **A/C/F NOT MAPPED — unchanged**, rationales as recorded (speculative-hop
  routing outside kernel boundary; cross-boundary repair owned by the
  application path; F needs obligation objects tracked for the
  rules/composition follow-up).
- **K5/K6/K8/K12 PASS, others per matrix — unchanged.** No new K
  regressions; the D4 proof-gaps work (claim-binding pins, caller-edge
  seam) touches verifier paths, not kernel decision paths.
- **Baseline pins resolved** (see table) — the only gate-row change, and
  it removes failures rather than adding coverage.

## Residuals carried (explicit, none silent)

C1-R1 (fixture placeholder digests, proven by C2 vectors, no gate
reliance); C2-R1 (real-world mapping authority open — B3 track);
C2-R2 (CALL_EDGE producer/checker divergence, needs separate amendment);
C2-R3 (premises-suite list revision via C3 supplement); C2-R4 (baseline
pins and untouched-file mypy noise — pins now resolved, mypy noise
remains out of scope); C3 R-C3-01–R-C3-04 mapping notes plus the
producer scope-visibility gap (same family as C2-R2); C4 none. G5
footnote carried verbatim from the disposition package: no publication-record
insert, status update, atomicity, interleaving, live-snapshot binding,
CAS/version-guard behavior, writer routing, RLS handling, contradiction
listing, or production behavior is claimed by the G4 evidence.

## Out of scope for this signature

Re-disposition (stays PARTIAL with direction); D3-family retesting
(Track A procedure, owner-triggered); any authoritative rollout or
production wiring (ARCH-CRIT-001 remains open by design until B2);
Estep pilots.

## Signature

- [ ] Owner sign-off: traceability matrix v1.0 (as reconciled above) is accepted as G4 A–T explicit-mapping evidence. Date / signature: _______________
