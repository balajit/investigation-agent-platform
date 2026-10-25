# D3 Stage C §6.2i Freeze Record v3 (SIGNED + HASHED — collection authorized under it)

> Status: **SIGNED 2026-10-27, HASHED. Owner sign-off: Balaji ("go", "owner sign-off + hashing").**
> No collection except under this record's run set and budget.
> Record hash (SHA-256 over exact bytes with the hash line below in its
> blank form `…recorded here: ________`): recorded here: ________
> Recompute rule: restore the hash line to blank, hash, compare.
> Any material change after hashing invalidates this freeze per §6.2i;
> replacement requires a new record, new run-set id, newly authorized budget.
>
> Controlling protocol: approval package v1.9 + Amendment A1 + Amendment A2
> (hierarchy v1.9+A1+A2 > v1.9+A1 > v1.9). Analyzer: proposal v1.15 /
> registry v12 (build-auth §8, owner-signed 2026-10-24, dev scope).
>
> Supersedes v2 (`ff0cf8e7…`, run set r2 verdict INSUFFICIENT kept open —
> retained as evidence, never merged). v3 freezes the caller-edge-hardened
> tree under unified verifier behavior with A2 eligibility: canonical
> caller-edge keys with pipe-escaping, class-stem + method matching, narrowed
> path-match exceptions, no-shadow-tap changes (see §1 commit note).

## 1. Code revision (TO BE PINNED AT SIGNING — tree currently dirty)

- Commit: `c9bedaf76f6db3de1b9d475bd483d5deafcca1fd` (code rev: secrets ports+infra fail-closed, caller-edge prompt sentence; owner-committed 2026-10-27).
- Working-tree reference: this record + manifest v3 + ledger are the only paths differing from that commit at signing; the signing commit freezes them (porcelain must read empty after it).
- Governed scope: all tracked paths; ignore rules per `.gitignore` (`tmp/`, `.venv/` excluded). `.env.local` is tracked, therefore governed, frozen at committed bytes (dev defaults).
- Drift rule: any porcelain output after hashing voids the freeze.
- Behavior id: `unified-dev-v1` (single active verifier behavior; historical `v1`/`v2`/`v3` labels audit-only, new rows unmarked).
- Protocol id: `v1.9+A1+A2` (eligibility per A2 OD-03/OD-04; r2's `verifier_version == v2` rule does not govern this run set).

## 2. Flag configuration

- `IAP_SHADOW_TYPED_DEFINITIONS=1` exactly at collection; `IAP_TYPED_DEFINITIONS`
  ABSENT (unset — not empty, not `"0"`, not any value). Any other value is a
  misconfiguration, never a mode. Observed at draft time: to be enforced per arm
  at collection start (flag set on shadow worker; control arm restarts with flag unset).

## 3. Fixtures, prompts, run-case list (unchanged shape, re-pinned in manifest v3)

- Run-case list: **all 10 battery cases** — L1, L2, L3, L4, L5, L8, L9, L10,
  L11, L12 in `tests/fixtures/battery_cases/`, exact bytes pinned in
  `docs/IAP-proof-kernel-d3-stageC-freeze-manifest-v3.json`
  (digests carried byte-identical from v2; no fixture change in this tree).
  No silent substitution; a fixture added later starts a new run set.
- Prompts: formulate-definition prompt pinned by file + content hash
  (template lives in code; no standalone prompt file exists):
  - `reasoning.py` (prompt `v3-claim-grounded-016`), SHA-256
    `ffee28d53a66efcf538587cfa7c9110b7c2192841390604cc4c232c701951bcd`
    (CHANGED vs v2 `3a79b37e…`/`014` — caller-edge `caller_edges` schema + per-edge binding + single-caller-incomplete sentence; verified at pinned commit)
  - `context_compiler.py` (prompt `v3-claim-grounded-015`), SHA-256
    `df9e3728234bf169137de3f2c723c8062df741bd497638607b8556a4ee22ad6e`
    (unchanged vs v2)
  (both files additionally pinned by item 1's commit at signing).

## 4. TAP comparison rule

- v1.9 §6.3 per-tap tuples as frozen there, as amended by A1/A2; no divergence.

## 5. Budget, control arm, exclusions (proposed — owner to confirm)

Budget: 12 reservations per frozen expected-cell bucket; 48 reservations
maximum run-set total (fresh budget for run set r3; r1's 6 and r2's 6 consumed
reservations belong to aborted/kept-open sets and are never merged). Each
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

Exclusions (v1.9 as amended by A1+A2): a recorded, turn-contained timeout whose
error outcome arrived through the normal channel is EXCLUDED (budget
consumed). An unrecorded timeout — or any timeout preventing recording,
corrupting ordering/identity/persistence/environment, or of unprovable
containment — voids the affected runs (artifacts retained, INSUFFICIENT
EVIDENCE, never trimmed/combined/rerun under that identity). A
superseded-attempt observation that is merely present is EXCLUDED from the
denominator and counted separately per §1.4, never joined; an attempted
cross-execution join, or inability to prove accepted-attempt identity, is an
ambiguous join and voids the run set. Non-eligible evaluations under A2
(wrong behavior/protocol id), runs with missing telemetry/conclude results for
non-counted reasons, runs outside the frozen environment, worker crash/restart,
unreachable store, telemetry pipeline failure, and ambiguous ordering or identity
are not exclusions — they void the run set.
Pre-consolidation rows are excluded by default per A2 OD-04 (no silent
reinterpretation; explicit re-baseline only if named in this record — none named).

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

## 6. Tenant + database identity (verified 2026-10-27; no credentials recorded)

- Host `localhost`, port `5452`, database `iap`, image
  `pgvector/pgvector:pg16`; server `PostgreSQL 16.15` (same frozen database
  as v1/v2; compose-stack database, read-only metadata queries).
- Dedicated probe tenant namespace: `iap-probe-2026-10-26-r3`. Verified empty
  and isolated 2026-10-27 (read-only): 0 rows for the candidate across
  `investigations` / `evidence` / `hypotheses` / `findings` /
  `investigation_conclusions` / `investigation_sessions` /
  `investigation_tool_executions` / `investigation_contradictions` /
  `investigation_transitions` / `action_executions` (dry-run scope audit,
  nothing deleted), 0 matching schemas in `pg_namespace`.
  r1 rows (`iap-probe-2026-10-24`) and r2 rows (`iap-probe-2026-10-24-r2`)
  retained as evidence, never merged.
  Isolation is by `tenant_id` scoping, not a separate database — recorded
  explicitly so no stronger isolation is assumed.

## 7. Telemetry and capture configuration

- Per v1.9 §1.4 step 7 / §6.2d as frozen there, as amended by A2 §A2-3:
  `loop_telemetry` per run (`shadow_definition_summary` counts incl.
  `dropped_count`, `duplicate_count`, `telemetry_omissions`, `mirror_notes`),
  bounded `definition_shadow_trace` projection, conclude co-location fields,
  omission accounting, envelope carries bound amendment id (`A2`). No capture
  surface beyond v1.5 §1.4 / v1.6 §12.1–§12.2.

## 8. Protocol hash + harness version

- Approval package v1.9: SHA-256
  `547aee50446c0fcc674a67e5f239e15de6dcf5f73de52474f3f133ce2be965c3`
- Amendment A1: SHA-256
  `c96ea6c8d5a97f7d75d664d4f1e109840c7a2b2316805dbbd2f629f6d4d7302f`
- Amendment A2: SHA-256
  `ddaa518b4a169049a4b44d1e10c3093e7d49e73941bf38e628855a0a6cbe1071`
  (working-tree bytes at draft; repin if A2 text changes before signing)
- Harness: proposal v1.15, registry `iap:d3-stagec:registry/v12`, vectors
  v1.15 (`810125cc87198a7f68eb88bd8ac959b6b77264d581c2f34c1362136ada2bfcf2`).

## 9. Decision owner + executing operator (owner-named 2026-10-27)

- Decision owner: **Balaji**. Executing operator: **Lead Engineer (muse)**.
- Powers per §6.2f: the operator may execute and abort; only the owner
  classifies results, authorizes further collection, or closes the probe
  as complete.

## Validation (executed 2026-10-27 on the completed record)

- [x] All nine items filled (no TBDs remain); owner sign-off recorded: Balaji, 2026-10-27 ("go", "owner sign-off + hashing").
- [x] Record hashed (SHA-256 over exact bytes with the hash line in its blank
  form `…recorded here: ________`); hash recorded here: `20b901d1291b7f2ff9fa7fdbf1eadb9079d8e7666291d4eb0e2e6961b4bc3419`
- [x] §6.2i checklist: v1.9+A1+A2 hierarchy acknowledged; validators green on
  the frozen basis before first collection — schema vectors 32 passed,
  signatures 52 passed, report cases 44 passed, D3 shadow suite 49 passed,
  full unit 3333 passed with 2 pre-existing baseline-rev pins, secrets suites
  41 passed, ruff check clean, fixtures 10/10 byte-identical, tenant
  `iap-probe-2026-10-26-r3` verified empty/isolated read-only.
- [ ] Run set opened only after this signing commit: `d3-stagec-2026-10-26-r3`, fresh 48 budget, freeze object + ledger head digests recorded at opening.
