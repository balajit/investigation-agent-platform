# Stage C run set d3-stagec-2026-10-24-r2 — collection log

Freeze record hash: ff0cf8e7d6d5166e6e2ff871fd6d6ca33d73afaad654a6ad0ca30706009e9d3f (commit 8d3f5f192f17114ef155b7e9702955dde3ddc96e).
Freeze object digest: sha256:c1166139ba7006b7705038c05f81ed75554be14baf479be98e1c852f618db0b5.
Ledger head: sha256:1a8720a1d955e7378a42d2376b4853bfd11654a3474acf556fe845f4f66a3956 (total 6/48).

## Expected-cell mapping (a priori)

L1-L5 expect ANSWERED terminals -> accept-PASS. L8 expects a clean ABSTAIN terminal -> nonaccept-PASS.
Mispredictions record as EXCLUDED/BUDGET_RESERVED_CELL_MISMATCH, never reassigned.

## Cohort prompt pin

Cohort prompt_hash is the formulate-definition prompt v3-claim-grounded-014 (case-driving prompt); both prompts are frozen globally in freeze.prompts.

## Shadow attempt ids (pre-computed, deterministic)

L1.json: sha256:bf64776e488c45e21cfa8e6b3857ab181c79c27c353af5c180ba02bcc6920aa2 (bucket accept-PASS)
L2.json: sha256:d7a29b1da19a2229c4710d3cc3ad07c0e18827a7317e522bcec8b4fb25ab40be (bucket accept-PASS)
L3.json: sha256:02ef6a2efcd9b3754ead5f989de8536f4250fe9948bc77f3c3905628a556c592 (bucket accept-PASS)
L4.json: sha256:6ba78d9d62aae6f0476a3fef7c132af1d739936119923f2d8344d22f05c7698e (bucket accept-PASS)
L5.json: sha256:356bc58f01f1c79628548e55cb7096df457f4309ceb3c81d2536ab773416d30c (bucket accept-PASS)
L8.json: sha256:6c47eb5244b468d6966bfce883f65c20a87dcf3c97b1dcaed9d78e1cb7354379 (bucket nonaccept-PASS)

## Control arm

After the shadow arm: restart worker with IAP_SHADOW_TYPED_DEFINITIONS unset; one control run per exercised fixture; k-th shadow pairs with k-th control (control_order = per-fixture ordinal); stop on native divergence. Control runs are excluded from the eligible denominator and consume no budget.

## Dev key

key_id dev-stagec-2026-10-24-01; public bytes (hex): 26708e51eaa950173b03bf23bac97132e190db55d64181fe98dce805d16d9192.
Private key at tmp/keys/d3-stagec-2026-10-24-r2.key (0600, gitignored, never committed).

## Verify (do NOT rerun casually: key valid_from refreshes the freeze)

sha256sum artifacts/<RS>/*.txt artifacts/<RS>/*.json  # compare to digests in freeze.json
(ledger attempt ids are deterministic in run_set_id+seq+fixture+case.)
