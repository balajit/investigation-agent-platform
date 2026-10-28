# G4 Re-disposition — DRAFT 2026-10-28 (for owner decision)

Status: DRAFT — proposed disposition is **PARTIAL, carrying exactly two opens**.
Supersedes nothing until signed; `docs/IAP-proof-kernel-g4-disposition-package-v1.0.md`
(DECIDED 2026-10-26, PARTIAL with direction) and the 2026-10-28 sign-off package
remain the record.

## §1 What changed since sign-off (`f4686c9`)

Wave-2 execution on `iap-full-1025-proof-kernel-closeout`, HEAD `b09ab84`:

- Obligation layer implemented + verified: `3af06d7` (R3b/R3c wiring),
  `46ec141` (R4b-d), `f8937d1` (write-path dedent fix — staging 10/16 → 16/16),
  `8b86d48` (R4b), `44e1a7b` (R4e), `6646558` (R4c/d), `a57986c` (R3c).
- D3 retested: `b09ab84` — 199 green (48 call-edge incl. every-site-pins-flag
  audit; 151 claim-binding family). Pin gating confirmed read-only
  (`strict = pin in (cbv2, cbv3)` at `verification.py:3491`; site 2 at `:3603`;
  site 1 inside the cbv2+-only `evaluate_caller_edge` entry).
- B1 opens closed this session: obligation implementation (R2 `bf9cf8c` +
  implementation + follow-ups), D3 retesting (above).

## §2 Verification state (evidence)

- Full unit: **4088 passed**, 4 skipped, 6 xfailed; only the 2 known
  pre-existing drift pins fail (`test_phase0_contract_repair`,
  `test_phase2_remaining`).
- Staging live proof-publication suite: **18/18**
  (`tests/integration/test_proof_publication_live.py`, `IAP_LIVE_TESTS=1`
  against `localhost:5452/iap`).
- Kernel wiring unit gate: 20 green; obligation gating 33 green (incl. R4b
  boundary); validator fingerprints 8/8 stable; ruff clean on all touched paths
  (21 pre-existing notes elsewhere: analyzer `python/` + intentional fixtures).
- Flags/production posture: `IAP_KERNEL_WIRING` default `off` (verified);
  `IAP_KERNEL_PUBLISH` unset; publish seam unwired (zero production callers);
  writer routing single-only by default.

## §3 Carried opens (exactly two)

### Open 1 — Legacy-writer closure (K13 traceability row; B1 residuals)
Heuristic fast paths + LWW writes remain outside the kernel's enforcement
boundary. Verified live on current tree:
`infrastructure/persistence/finding_repository.py:217`
(`save_conclusion`, `.on_conflict_do_update(...)`);
`hypothesis_repository.py:97-130` (no-CAS LWW). The guarded
`ProofPublicationService.commit` is the sole writer to the proof-commit tables
(caller audit 2026-10-28), but conclusions/hypotheses stay reachable through the
old paths. Close conditions (either): (a) gate or remove the LWW paths
(implementation, separate GO — the worker flow depends on them); or
(b) explicit owner risk-acceptance.

### Open 2 — C2-R1 real-world mapping authority (disposition §3; ledger 586/603/721)
Synthetic-only `BUILD_SOURCE_MAPPING` authority approved test-only
(ledger 586); Component 2 accepted with this residual (603); B3 mechanism
(frozen registry + Ed25519) PROVISIONAL, reopenable only on a named consumer or
specific threat (721). No verification activity can close a trust-anchor
decision. Close conditions (either): (a) name the consumer/threat → reopen B3;
or (b) explicit owner acceptance/deferral.

## §4 Held (not opens — no action, recorded for completeness)
- Live-battery parity gates (R3b shadow-vs-off, D2/D3 no-flip): need full
  stack + live spend; offline + staging evidence stands in.
- Production enforcing flip: needs worker stack + operator policy + separate GO.
- Branch push: needs explicit GO.

## §5 Proposed decision (owner to mark)

- [ ] (a) G4 PASS, scoped to §2 evidence with §3 residuals carried.
- [ ] (b) G4 PARTIAL, carrying the two §3 opens (recommended — no new
      evidence closes either open; both need decisions/implementation outside
      verification).
- [ ] (c) Other (record verbatim).
