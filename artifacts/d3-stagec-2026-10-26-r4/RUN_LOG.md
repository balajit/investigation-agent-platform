# Stage C run set d3-stagec-2026-10-26-r4 — collection log

Freeze record hash: 6dcae60085402c4045d7b3e64a6949d422c7fb6dde82b96b33a88a42f1fde5bb (commit eeecfae0d82d15b5f5439a49c7653a4a14669bb7).
Freeze object digest: sha256:5d8388a3fe4e6eaf5af0436dfacbbd15aa0e58a1e6160d998f9b9a07102193ed.
Ledger head: sha256:07fe393a6c25e38b16881f7e93268c44eb838a4ab1157669cdfd389f30600da4 (total 10/48).

## Expected-cell mapping (a priori)

L1-L5 expect ANSWERED terminals -> accept-PASS. L8 expects a clean ABSTAIN terminal -> nonaccept-PASS.
Mispredictions record as EXCLUDED/BUDGET_RESERVED_CELL_MISMATCH, never reassigned.

## Cohort prompt pin

Cohort prompt_hash is the formulate-definition prompt v3-claim-grounded-016 (case-driving prompt); both prompts are frozen globally in freeze.prompts.

## Shadow attempt ids (pre-computed, deterministic)

L1.json (seq 1, L1 rep 1): sha256:487b73ca5dc758f3f5b913397832d5765dc593ce7dd53832ec860e07e6d8e4de (bucket accept-PASS)
L2.json (seq 2, L2 rep 1): sha256:0e44dec1c1c7ecda99eccf553de1abaae82f1eb61462b381db31b8ad04ee2795 (bucket accept-PASS)
L3.json (seq 3, L3 rep 1): sha256:11e841b154acab1eaeb9943ed273d6d641a307220cf8f2613dd2430f3b03d81b (bucket accept-PASS)
L4.json (seq 4, L4 rep 1): sha256:974d08f6fe3a304d9b38045e94e653689a2b0eae55012b54ac07d02406aa1433 (bucket accept-PASS)
L5.json (seq 5, L5 rep 1): sha256:e2de7c37075ff9eba9252a245522c58807383f231b2e7f0caa8df992a8547625 (bucket accept-PASS)
L8.json (seq 6, L8 rep 1): sha256:4b2753eeaa03158c0c93ceda2690f7febda12d9d4a6729523612845c7dabbfbd (bucket nonaccept-PASS)
L2.json (seq 7, L2 rep 2): sha256:da1bbc78081cc96c3e979edd57f52ed07b41ac3106918cf2df2152a96cfa4f81 (bucket accept-PASS)
L2.json (seq 8, L2 rep 3): sha256:ef36baf33748c72e6ece4aa83783eb1d97e3c3290c0f4b07bedaaad10dccb1ad (bucket accept-PASS)
L2.json (seq 9, L2 rep 4): sha256:3395bb462ef80ce63c5422f753f685cf3abe1beca942b4f6680be3bf55c1ed2b (bucket accept-PASS)
L2.json (seq 10, L2 rep 5): sha256:41140b967420af321d1bc2a6f76335f061784f6fb21c6927ad6778ba0457a3e8 (bucket accept-PASS)

## Control arm

After the shadow arm: restart worker with IAP_SHADOW_TYPED_DEFINITIONS unset; one control run per exercised fixture; k-th shadow pairs with k-th control (control_order = per-fixture ordinal); stop on native divergence. Control runs are excluded from the eligible denominator and consume no budget.

## Dev key

key_id dev-stagec-2026-10-26-r3; public bytes (hex): 34e3abc5342bcbf1e019c57e720b5413269254571b3736282cb4df02e3b01c3f.
Private key at tmp/keys/d3-stagec-2026-10-26-r4.key (0600, gitignored, never committed).

## Verify (do NOT rerun casually: key valid_from refreshes the freeze)

sha256sum artifacts/<RS>/*.txt artifacts/<RS>/*.json  # compare to digests in freeze.json
(ledger attempt ids are deterministic in run_set_id+seq+fixture+case.)

## 2026-10-26 collection authorization (owner sign-off recorded here)

- Freeze record v4 hash: 6dcae60085402c4045d7b3e64a6949d422c7fb6dde82b96b33a88a42f1fde5bb (blank-line rule; tree clean at signing commits eeecfae + 36b9732).
- Owner sign-off: "Signed — build and collect" (Step 11 session, 2026-10-26).
- Budget authorized: fresh 48, 12 per frozen expected-cell bucket.
- Collection plan: 10 shadow attempts (L2 x5 + L1,L3,L4,L5,L8) + 6 flag-off controls (1/fixture, first-attempt pairing for L2).
