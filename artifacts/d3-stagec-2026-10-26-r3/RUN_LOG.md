# Stage C run set d3-stagec-2026-10-26-r3 — collection log

Freeze record hash: 20b901d1291b7f2ff9fa7fdbf1eadb9079d8e7666291d4eb0e2e6961b4bc3419 (commit 95eab4b70abf9283f899890a3d12ca0bf57f4ec6).
Freeze object digest: sha256:902af42c1aa2b0856d81845dcaed96c769df9f95874e311c2c94eff95ca045bb.
Ledger head: sha256:9c0223abbe14503f936446303d3f234e9b2cd2981f70665723e8f4fe204d572e (total 6/48).

## Expected-cell mapping (a priori)

L1-L5 expect ANSWERED terminals -> accept-PASS. L8 expects a clean ABSTAIN terminal -> nonaccept-PASS.
Mispredictions record as EXCLUDED/BUDGET_RESERVED_CELL_MISMATCH, never reassigned.

## Cohort prompt pin

Cohort prompt_hash is the formulate-definition prompt v3-claim-grounded-016 (case-driving prompt); both prompts are frozen globally in freeze.prompts.

## Shadow attempt ids (pre-computed, deterministic)

L1.json: sha256:e2a5194516bf3b4ab9b1074f2eb6b4b2fb1830c611ef6f9b863c914a7dcfee74 (bucket accept-PASS)
L2.json: sha256:505889bf509a190c8eeeff7b90be65f0a65c4e4a6290936bf9d158477cf711c0 (bucket accept-PASS)
L3.json: sha256:017a057d76add3a2e90ad390b2c3854f74c0abcaf02cf88c47abc2c4fbfc1106 (bucket accept-PASS)
L4.json: sha256:3eb10d28c651db12de867619d602c1ba13734ec9cd774dc8bd44b13547d0062d (bucket accept-PASS)
L5.json: sha256:994175bc20ea401a8a0bd00b6523c9fe4e0a69380b9fae84e442487418c6168a (bucket accept-PASS)
L8.json: sha256:f028f0c11bb9534aea7724ce57417f98f1288ef8994a6c0e7441c44a2f9961e6 (bucket nonaccept-PASS)

## Control arm

After the shadow arm: restart worker with IAP_SHADOW_TYPED_DEFINITIONS unset; one control run per exercised fixture; k-th shadow pairs with k-th control (control_order = per-fixture ordinal); stop on native divergence. Control runs are excluded from the eligible denominator and consume no budget.

## Dev key

key_id dev-stagec-2026-10-26-r3; public bytes (hex): 79a1e760f118080cedc311ba6617059d950b5eb013d53426e8b11dabb2fe6c80.
Private key at tmp/keys/d3-stagec-2026-10-26-r3.key (0600, gitignored, never committed).

## Verify (do NOT rerun casually: key valid_from refreshes the freeze)

sha256sum artifacts/<RS>/*.txt artifacts/<RS>/*.json  # compare to digests in freeze.json
(ledger attempt ids are deterministic in run_set_id+seq+fixture+case.)

## 2026-10-25 19:05 UTC — shadow arm executed (6/6) — operator
- Battery pid 94058, tag 20261025T185850, tenant iap-probe-2026-10-26-r3, profile spring-sample-service auto-created v1.
- Worker: single poller from this repo, IAP_SHADOW_TYPED_DEFINITIONS=1 confirmed; API READY.
- Verdicts: L1 FAIL, L2 FAIL, L3 FAIL, L4 FAIL, L5 FAIL (all COMPLETED + INSUFFICIENT_EVIDENCE, want answered), L8 PASS (honest abstention). Report: reports/d3-stagec-2026-10-26-r3-shadow.md (gitignored scratch).
- DB capture (read-only): 6/6 hyp UNDER_INVESTIGATION, claim_kinds ABSENCE-only (13 total), 0 untyped assessments, absence proofs scoped (absence:///mcp://, no fabricated CEL spans), chains=0 everywhere. Reasoner 016 + compiler 015 confirmed in play (frozen rev).
- Budget: 6 spent (r3-s01..s06).

## 2026-10-25 19:10 UTC — control arm executed (6/6, flag off) — operator
- Worker restarted flag-unset (single poller 94644/94648, flag absent confirmed, API READY); battery pid 94684, tag 20261025T190516, --no-cleanup-prior (shadow rows preserved).
- Verdicts: IDENTICAL pattern — L1-L5 FAIL (INSUFFICIENT_EVIDENCE, same bucket "scoped absence proof banked but heuristic score / chain gate abstains"), L8 PASS. Report: reports/d3-stagec-2026-10-26-r3-control.md (gitignored scratch).
- DB capture: 6/6 UNDER_INVESTIGATION, ABSENCE-only (14 total), 0 untyped assessments EVEN WITH FLAG OFF, scoped proofs, chains=0, revision drift none.
- Parity shadow-vs-control: EXACT on all six (verdict, status, hyp state, bucket, kinds, typing). D3-anomaly screen clean both arms: no untyped, no fabricated spans, no paraphrase vectors (no chains), no drift.
- Budget: 6 spent (procedure-required 1:1 controls, r2 pattern; PLAN §5 6-reservation covered shadow). Total probe spend 12/48 → 36 remaining.

## 2026-10-25 19:15 UTC — DECISION: NO-GO (probe-level, not a fix rejection) — operator
- Acceptance bar (5 accept-PASS + parity + anomaly absence): parity ✓, anomaly absence ✓, but L1-L5 abstained in BOTH arms — contradicts r1 baseline (014-era, 5 accept-PASS). Cause is shared across arms → NOT the shadow flag.
- Candidates (undetermined in this design): 016 reasoner sentence over-triggers absence framing, azure model drift since r1, corpus/index change. Evidence retrieved (1-8 items/run) but hypotheses form ABSENCE and chain gate abstains.
- Fix status: parity exact + zero anomalies ⇒ the caller-edge change introduces no divergence; NOT rejected. D3 stays open: acceptance still unmet, close-out requires a probe where accept fixtures answer AND parity holds.
- Next: planned diagnosis (prompt-ablation 016-vs-014 and/or model-version check on a scratch tenant) via new PLAN amendment — no improvisation. Probe tenant rows RETAINED as evidence. Worker restored flag-on steady state.
