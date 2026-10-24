# D3 Stage C §6.2i Freeze Record v2 (SIGNED + HASHED — collection authorized under it)

> Status: **SIGNED 2026-10-24, HASHED. Owner sign-off: Balaji ("go to the next phase").**
> Record hash (SHA-256 over exact bytes with the hash line below in its
> blank form `…recorded here: ________`): recorded in the hash field.
> Recompute rule: restore the hash line to blank, hash, compare.
> Any material change after hashing invalidates this freeze per §6.2i;
> replacement requires a new record, new run-set id, newly authorized budget.
>
> Controlling protocol: approval package v1.9 + Amendment A1 (hierarchy
> v1.9+A1 > v1.9). Analyzer: proposal v1.15 / registry v12 (build-auth §8,
> owner-signed 2026-10-24, dev scope).
>
> Supersedes v1 (`9be13fa7…`, run set r1 ABORTED on the INVALID_FLOAT
> capture defect — retained as evidence). v2 freezes the certified tree:
> float normalization at the envelope boundary, loud + counted pairing
> failures, T2/T3 mirror-note coverage counting, N21 telemetry absence,
> T3 conclude-gate pairing (owner-certified live 2026-10-24: 50
> shadow-suite tests green, scratch battery 6/6 with envelopes,
> flag-off control native parity + D3 key absence).

## 1. Code revision

- Commit: `8d3f5f192f17114ef155b7e9702955dde3ddc96e`
- `git status --porcelain`: empty (verified at draft time).
- Governed scope: all tracked paths; ignore rules per `.gitignore`
  (`tmp/`, `.venv/` excluded). `.env.local` is tracked, therefore
  governed, frozen at committed bytes (dev defaults).
- Drift rule: any porcelain output after hashing voids the freeze.

## 2. Flag configuration

- `IAP_SHADOW_TYPED_DEFINITIONS=1` exactly at collection; `IAP_TYPED_DEFINITIONS`
  ABSENT (unset — not empty, not `"0"`, not any value). Any other value is a
  misconfiguration, never a mode. Observed at draft time: flag set on the
  collection worker (shadow-ready); control arm restarts the worker with the
  flag unset; enforced per arm at collection start.

## 3. Fixtures, prompts, run-case list (owner-confirmed 2026-10-24)

- Run-case list: **all 10 battery cases** — L1, L2, L3, L4, L5, L8, L9, L10,
  L11, L12 in `tests/fixtures/battery_cases/`, exact bytes pinned in
  `docs/IAP-proof-kernel-d3-stageC-freeze-manifest-v2.json`
  (manifest SHA-256 `b9a1698a6e86d3c8f4ce674912677956f6637a85a9c09109d0fc7f50e18a1cae`;
  `battery_case_digests`, 10/10 recomputed and byte-identical to v1).
  No silent substitution; a fixture added later starts a new run set.
- Prompts: exact formulate-definition prompt pinned by file + content hash
  (template lives in code; no standalone prompt file exists):
  - `reasoning.py` (prompt `v3-claim-grounded-014`), SHA-256
    `3a79b37e2b29dc994255be081469926d9a59459e02a692f6ab688862c9ab9609`
  - `context_compiler.py` (prompt `v3-claim-grounded-015`), SHA-256
    `df9e3728234bf169137de3f2c723c8062df741bd497638607b8556a4ee22ad6e`
  (both files additionally pinned by item 1's commit; both byte-identical
  to v1).

## 4. TAP comparison rule

- v1.9 §6.3 per-tap tuples as frozen there; no divergence.

## 5. Budget, control arm, exclusions (frozen per owner direction 2026-10-24)

Budget: 12 reservations per frozen expected-cell bucket; 48 reservations
maximum run-set total (fresh budget for run set r2; r1's 6 consumed
reservations belong to the aborted set and are never merged). Each
reservation is charged exactly once to its frozen expected-cell bucket at
reservation time. An attempt whose observed outcome maps to a different
cell is recorded as EXCLUDED against the reserved bucket
(reason `BUDGET_RESERVED_CELL_MISMATCH`) and is never reassigned, refunded,
or transferred. A retry is an attempt and consumes the same frozen budget; no
separate retry allowance, no resets, no extensions. Observed-cell counts are
diagnostic only and subject to no cap.

Control arm: exactly one shadow-off control run per fixture exercised in the
shadow arm, never more control runs than shadow attempts on the same fixture
(§6.2g). The exact native/shadow run-pairing and sequence evidence is frozen
per the control-artifact spec below.

Exclusions (v1.9 as amended by A1): a recorded, turn-contained timeout whose
error outcome arrived through the normal channel is EXCLUDED (budget
consumed). An unrecorded timeout — or any timeout preventing recording,
corrupting ordering/identity/persistence/environment, or of unprovable
containment — voids the affected runs (artifacts retained, INSUFFICIENT
EVIDENCE, never trimmed/combined/rerun under that identity). A
superseded-attempt observation that is merely present is EXCLUDED from the
denominator and counted separately per §1.4, never joined; an attempted
cross-execution join, or inability to prove accepted-attempt identity, is an
ambiguous join and voids the run set. Non-v2 evaluations, runs with missing
telemetry/conclude results for non-counted reasons, runs outside the frozen
environment, worker crash/restart, unreachable store, telemetry pipeline
failure, and ambiguous ordering or identity are not exclusions — they void
the run set.

Materiality: any change to code, flags, fixtures, prompts, run-case list,
budget, tenant/database, telemetry/capture configuration, harness version, or
protocol requires a new freeze; affected collection is discarded (retained
with old identity, never merged).

Control-artifact spec (bound by `control_hash` over the frozen
`control_preimage`): per fixture, the frozen control artifact names the
native attempt id and shadow attempt/run id, both `workflow_run_id`s,
iteration/order positions, and the digest chain linking them — positions in
the ordered receipt chain per §6.2d, never the self-reported `control_order`
integers. Audit, executed before any verdict: re-derive the pairing from
workflow history and compare against the frozen pairing; any mismatch voids
the run set.

## 6. Tenant + database identity (owner-confirmed 2026-10-24; no credentials recorded)

- Host `localhost`, port `5452`, database `iap`, image
  `pgvector/pgvector:pg16`; server `PostgreSQL 16.15 (Debian
  16.15-1.pgdg12+2) on aarch64-unknown-linux-gnu` (same frozen database
  as v1; compose-stack database, read-only metadata queries).
- Dedicated probe tenant namespace: `iap-probe-2026-10-24-r2`. Verified unused
  and isolated 2026-10-24: zero rows for the candidate across
  `action_executions` / `evidence` / `investigations` / `hypotheses` /
  `findings`, no such schema in `pg_namespace`. r1 rows remain under
  `iap-probe-2026-10-24`, retained as aborted-run evidence, never merged.
  Isolation is by `tenant_id` scoping, not a separate database — recorded
  explicitly so no stronger isolation is assumed.

## 7. Telemetry and capture configuration

- Per v1.9 §1.4 step 7 / §6.2d as frozen there: `loop_telemetry` per run
  (`shadow_definition_summary` counts incl. `dropped_count`,
  `duplicate_count`, `telemetry_omissions`, `mirror_notes`), bounded
  `definition_shadow_trace` projection, conclude co-location fields, omission
  accounting. No capture surface beyond v1.5 §1.4 / v1.6 §12.1–§12.2.

## 8. Protocol hash + harness version

- Approval package v1.9: SHA-256
  `547aee50446c0fcc674a67e5f239e15de6dcf5f73de52474f3f133ce2be965c3`
- Amendment A1: SHA-256
  `c96ea6c8d5a97f7d75d664d4f1e109840c7a2b2316805dbbd2f629f6d4d7302f`
- Harness: proposal v1.15, registry `iap:d3-stagec:registry/v12`, vectors
  v1.15 (`810125cc87198a7f68eb88bd8ac959b6b77264d581c2f34c1362136ada2bfcf2`).

## 9. Decision owner + executing operator (owner-named 2026-10-24)

- Decision owner: **Balaji**. Executing operator: **Lead Engineer (muse)**.
- Powers recorded separately per §6.2f: the operator may execute and abort;
  only the owner classifies results, authorizes further collection, or
  closes the probe as complete.

## Validation (executed 2026-10-24 on the completed record)

- [x] All nine items filled (no TBDs remain); owner sign-off recorded: Balaji, 2026-10-24 ("go to the next phase").
- [x] Record hashed (SHA-256 over exact bytes with the hash line in its blank
  form `…recorded here: ________`); hash recorded here: `ff0cf8e7d6d5166e6e2ff871fd6d6ca33d73afaad654a6ad0ca30706009e9d3f`.
- [x] §6.2i checklist: all nine items present; v1.9+A1 hierarchy acknowledged;
      validators green on the frozen basis (32 + 44 + 52 gates, CLI --all 44,
      147 harness tests, 50 shadow-suite tests, full unit 3327 passed with
      2 pre-existing baseline-rev pins) before first collection.
