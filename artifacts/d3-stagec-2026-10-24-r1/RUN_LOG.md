# Stage C run set d3-stagec-2026-10-24-r1 — collection log

Freeze record hash: 9be13fa7b9f730655aac28797218dca06f076c499feb802dcb0de57e95b99dc1 (commit fdb9e4a121339f8fad49f23f977d32b43de55c2e).
Freeze object digest: sha256:8e2189e9e135a4b2cfb7f4c16a398f719d9d88adb5f83dcbfa50bdc0d285ae35.
Ledger head: sha256:7710e502da17c01bc0d1d0ca8d9c31805cfa5e227960477c1b4004e05f20e24b (total 6/48).

## Expected-cell mapping (a priori)

L1-L5 expect ANSWERED terminals -> accept-PASS. L8 expects a clean ABSTAIN terminal -> nonaccept-PASS.
Mispredictions record as EXCLUDED/BUDGET_RESERVED_CELL_MISMATCH, never reassigned.

## Cohort prompt pin

Cohort prompt_hash is the formulate-definition prompt v3-claim-grounded-014 (case-driving prompt); both prompts are frozen globally in freeze.prompts.

## Shadow attempt ids (pre-computed, deterministic)

L1.json: sha256:242e2427a0bfef151095b04b1765e4f407531b06c9186e58e34f002a3332e519 (bucket accept-PASS)
L2.json: sha256:b42ab0e95986db02194ffc0dd1e492d8e528cb972bb7b3960a897ed39a039bad (bucket accept-PASS)
L3.json: sha256:0923c7f2bb0ad6d593b17c0496fa94acd3f7a401170defd1d9371abe70ce213a (bucket accept-PASS)
L4.json: sha256:8daffe75b16449871e706bdc451b6217b9c16c40f9ba79fa4fb169be86805984 (bucket accept-PASS)
L5.json: sha256:572112faf76bfb949f0eed2b5d318c71472789439a3ef3b7a6c82faa4c6df4a5 (bucket accept-PASS)
L8.json: sha256:ec4a9f688689a1eca8be380e9b784e50101c9ee9e652438abf205398aaf2be12 (bucket nonaccept-PASS)

## Control arm

After the shadow arm: restart worker with IAP_SHADOW_TYPED_DEFINITIONS unset; one control run per exercised fixture; k-th shadow pairs with k-th control (control_order = per-fixture ordinal); stop on native divergence. Control runs are excluded from the eligible denominator and consume no budget.

## Dev key

key_id dev-stagec-2026-10-24-01; public bytes (hex): de80d05ac195ae46934ba3f84c0ece4ffbf42a7c2d2eccd80c7f2ef9b170e1d6.
Private key at tmp/keys/d3-stagec-2026-10-24-r1.key (0600, gitignored, never committed).

## Verify (do NOT rerun casually: key valid_from refreshes the freeze)

sha256sum artifacts/<RS>/*.txt artifacts/<RS>/*.json  # compare to digests in freeze.json
(ledger attempt ids are deterministic in run_set_id+seq+fixture+case.)

## Shadow arm execution (live)
- Battery tag: 20261024T204835 (logs: tmp/shadow_arm.log, report: reports/d3-stagec-2026-10-24-r1-shadow.md).
- Plan order = reservation order: L1#seq1, L2#seq2, L3#seq3, L4#seq4, L5#seq5, L8#seq6.
- Worker: single poller with IAP_SHADOW_TYPED_DEFINITIONS=1 (verified via ps eww).
- 2026-10-24 ~20:52 UTC: L1/L2/L3 COMPLETED+PASS; loop_telemetry carries
  shadow keys (mode active);   shadow_summary.turns=0 on all three so far.
  Eligibility impact assessment deferred to post-arm capture review (owner classifies).

## COLLECTION ABORTED 2026-10-24 ~21:05 UTC — systematic capture defect (operator abort)
- Shadow arm executed 6/6 (seq 1-6, tag 20261024T204835): L1/L2/L4/L5 PASS,
  L3 FAIL (INSUFFICIENT_EVIDENCE + revision drift), L8 PASS (honest abstention).
  All runs recorded normally; no void-class event.
- Post-arm audit of Temporal histories: verify activity DID emit
  shadow_definitions observations (e.g., L1 sched 185: 2 obs; L8: 2 verify
  results with obs), but the workflow pairing block
  (workflows.py receipt-pairing, `pair_receipt` -> `build_correlation_envelope`)
  raises `CanonicalizationError INVALID_FLOAT: $.native_outcome.confidence`
  on every observation: the native confidence float is passed into canonical
  bytes, which forbid floats. The block's bare `except Exception: pass`
  swallows the failure, so `definition_shadow_trace` is empty and
  `shadow_definition_summary.turns` is 0 in all 6 runs — with
  capture_errors reported as 0 (vacuously true; the loss is unmarked).
- Consequence: zero eligible turns are producible under this frozen code
  revision. The §6.1/§6.3 floor (≥20 eligible, ≥3/cell) is unmeetable, so
  the run-set verdict is INSUFFICIENT EVIDENCE by construction. Further
  spend (controls, repeats) cannot change it.
- Operator action: ABORT (powers per freeze item 9). Remaining budget
  (42/48) unspent; 6 reservations stand as recorded evidence. Control arm
  NOT executed (no shadow counterparts exist in envelope form).
- Required path (owner decision): code fix (confidence handling in the
  pairing/envelope path + accounted capture failure instead of silent pass)
  → new code revision → NEW freeze, NEW run-set id, newly authorized budget
  per §§6.2e–6.2f/§6.3. Any code change now is material to freeze 9be13fa7.
- Reproduction (read-only, no freeze impact): decode any completed run's
  verify-result payload from Temporal history
  (`wf-investigation-<uuid>`) and call `pair_receipt` with the embedded
  `shadow_definitions` + native outcome incl. float confidence.
