# D3 Stage C §6.2i Freeze Record v4 (PENDING OWNER SIGN-OFF)

> Status: **PENDING OWNER SIGN-OFF. NO COLLECTION under this record until the
> owner confirms the record hash below and the confirmation is recorded in
> the run-set RUN_LOG.**
> Record hash (SHA-256 over exact bytes with the hash line below in its
> blank form `…recorded here: ________`): recorded here: ________
> Recompute rule: restore the hash line to blank, hash, compare. Sign-off
> applies to the hash value, not to any status word in these bytes.
> Any material change after hashing invalidates this freeze per §6.2i;
> replacement requires a new record, new run-set id, newly authorized budget.
>
> Controlling protocol: approval package v1.9 + Amendment A1 + Amendment A2
> (hierarchy v1.9+A1+A2 > v1.9+A1 > v1.9; DEC-2026-10-26-A2 owner-approved).
> Analyzer: proposal v1.15 / registry v12 (build-auth §8, owner-signed
> 2026-10-24, dev scope).
>
> Supersedes v3 (`20b901d1…`, run set r3 verdict NO-GO 2026-10-25 retained as
> evidence — L1–L5 abstained in both arms on ABSENCE-only hypotheses;
> never merged). v4 freezes the retrieval-repair tree that unblocked L2:
> hypothesis persistence for every action type, CALL_EDGE revision pin from
> the bound citation, call-edge causal synthesis in the conclude gate, and
> the verify `no_hypotheses` skipped envelope — all verified live on scratch
> (Step 10, both arms PASS with native parity). Story-log envelopes are
> observability-only (message/data keys byte-identical) and change no
> verdict path.

## 1. Code revision (pinned at signing)

- Commit: `eeecfae0d82d15b5f5439a49c7653a4a14669bb7` (code rev: Step-11
  remediation tree — hypothesis persistence for all actions; CALL_EDGE
  revision pin; call-edge causal synthesis; verify skipped envelope; worker
  split into activities/workflows packages with re-export shims; config-driven
  story logging; story pass-through in evidence/context-compiler; r4-adapted
  run-set builder; owner-directed session files `1` +
  `scripts/dev-shadow-certify.sh` committed as tooling).
- Working-tree reference at signing: this record + manifest v4 + remediation
  plan + ledger are the only paths differing from that commit; the signing
  commit freezes them (porcelain must read empty after it, except the
  explicitly excluded living log — see living-log rule below).
- Governed scope: all tracked paths; ignore rules per `.gitignore` (`tmp/`,
  `.venv/`, `reports/`, `data/*` excluded). `.env.local` is tracked,
  therefore governed, frozen at committed bytes (dev defaults, unchanged).
- Living-log rule (precedent r1–r3): `docs/IAP_Arch_Eng_Recommendations.log`
  is append-only operations evidence, not frozen bytes. The freeze hash
  covers this record only; ledger lines are carried uncommitted and never
  rewritten. Run-set artifacts (`artifacts/<run-set>/`) are committed as
  evidence after the run-set build without voiding the freeze (governed
  bytes unchanged; enforced by the builder's governed-path check).
- Drift rule: any porcelain output on governed paths after hashing voids
  the freeze.
- Behavior id: `unified-dev-v1` (single active verifier behavior; historical
  `v1`/`v2`/`v3` labels audit-only, new rows unmarked).
- Protocol id: `v1.9+A1+A2` (eligibility per A2 OD-03/OD-04; r2's
  `verifier_version == v2` rule does not govern this run set).

## 2. Flag configuration

- `IAP_SHADOW_TYPED_DEFINITIONS=1` exactly at collection; `IAP_TYPED_DEFINITIONS`
  ABSENT (unset — not empty, not `"0"`, not any value). Any other value is a
  misconfiguration, never a mode. Observed at draft time: to be enforced per arm
  at collection start (flag set on shadow worker; control arm restarts with flag unset).

## 3. Fixtures, prompts, run-case list (unchanged shape, re-pinned in manifest v4)

- Run-case list: **all 10 battery cases** — L1, L2, L3, L4, L5, L8, L9, L10,
  L11, L12 in `tests/fixtures/battery_cases/`, exact bytes pinned in
  `docs/IAP-proof-kernel-d3-stageC-freeze-manifest-v4.json`
  (digests verified byte-identical to v3 on 2026-10-26; no fixture change).
  No silent substitution; a fixture added later starts a new run set.
- Collection executes the 6 live fixtures per the r3 precedent: L1–L5 plus
  L8, with L2 as the repeated primary (see §5). L9–L12 run offline-only.
- Prompts: formulate-definition prompt pinned by file + content hash
  (template lives in code; no standalone prompt file exists):
  - `reasoning.py` (prompt `v3-claim-grounded-016`), SHA-256
    `277ed0ad5caa31ec80a96a2bcd1a44d334d026ac979e07a636503906499259ef`
    (versions unchanged vs v3; bytes re-pinned — content identical to the
    v3-era tree modulo the frozen remediation; verified at pinned commit)
  - `context_compiler.py` (prompt `v3-claim-grounded-015`), SHA-256
    `40f9384799f62f39ec101c096ffdbb3f93df191a209b9f517d39977663d43da3`
    (versions unchanged vs v3; the +26-line story excerpt block is additive
    rendering, prompt template untouched; verified at pinned commit)
  (both files additionally pinned by item 1's commit at signing).

## 4. TAP comparison rule

- v1.9 §6.3 per-tap tuples as frozen there, as amended by A1/A2; no divergence.

## 5. Budget, control arm, exclusions (proposed — owner to confirm)

Budget: 12 reservations per frozen expected-cell bucket; 48 reservations
maximum run-set total (fresh budget for run set r4; r1/r2/r3 spends belong to
aborted/kept-open sets and are never merged). Each reservation is charged
exactly once to its frozen expected-cell bucket at reservation time. An
attempt whose observed outcome maps to a different cell is recorded as
EXCLUDED against the reserved bucket (reason
`BUDGET_RESERVED_CELL_MISMATCH`) and is never reassigned, refunded,
transferred. A retry is an attempt and consumes the same frozen budget; no
separate retry allowance, no resets, no extensions. Observed-cell counts are
diagnostic only and subject to no cap.

Shadow arm (10 attempts): L2 ×5 (repeat-stability for the binomial CI) plus
L1, L3, L4, L5, L8 ×1. Buckets: 9 accept-PASS + 1 nonaccept-PASS, each ≤ 12.
Control arm: exactly one control run per fixture exercised (6 runs, no
budget), 1:1 per fixture per §6.2g. Pairing: k-th shadow pairs with k-th
control; L2 shadows k=1..5, control k=1 pairs with shadow k=1 (first-attempt
pairing); k=2..5 are repeat-stability runs without control counterparts.
Stop on any fresh control divergence or capture/provenance violation.

Exclusions (v1.9 as amended by A1+A2): a recorded, turn-contained timeout
whose error outcome arrived through the normal channel is EXCLUDED (budget
consumed). An unrecorded timeout — or any timeout preventing recording,
corrupting ordering/identity/persistence/environment, or of unprovable
containment — voids the affected runs (artifacts retained, INSUFFICIENT
EVIDENCE, never trimmed/combined/rerun under that identity). A
superseded-attempt observation that is merely present is EXCLUDED from the
denominator and counted separately per §1.4, never joined; an attempted
cross-execution join, or inability to prove accepted-attempt identity, is an
ambiguous join and voids the run set. Non-eligible evaluations under A2
(wrong behavior/protocol id), runs with missing telemetry/conclude results
for non-counted reasons, runs outside the frozen environment, worker crash/
restart, unreachable store, telemetry pipeline failure, and ambiguous
ordering or identity are not exclusions — they void the run set.
Pre-consolidation rows are excluded by default per A2 OD-04 (no silent
reinterpretation; none named in this record).

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

## 6. Tenant + database identity (verified 2026-10-26; no credentials recorded)

- Host `localhost`, port `5452`, database `iap`, image
  `pgvector/pgvector:pg16`; server PostgreSQL 16.15 (same frozen database
  as v1/v2/v3; compose-stack database, read-only metadata queries).
- Dedicated probe tenant namespace: `iap-probe-2026-10-26-r4`. Verified empty
  and isolated 2026-10-26 (read-only; 0 rows across `investigations` /
  `evidence` / `hypotheses` / `findings` / `investigation_conclusions` /
  `investigation_sessions` / `investigation_tool_executions` (via
  investigation join) / `investigation_contradictions` /
  `investigation_transitions` /   `action_executions`; nothing deleted). r1/r2/r3 probe rows retained as
  evidence, never merged.
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
  (working-tree bytes, verified 2026-10-26)
- Amendment A1: SHA-256
  `c96ea6c8d5a97f7d75d664d4f1e109840c7a2b2316805dbbd2f629f6d4d7302f`
  (working-tree bytes, verified 2026-10-26)
- Amendment A2: SHA-256
  `ddaa518b4a169049a4b44d1e10c3093e7d49e73941bf38e628855a0a6cbe1071`
  (working-tree bytes, verified 2026-10-26)
- Harness: proposal v1.15, registry `iap:d3-stagec:registry/v12`, vectors
  v1.15 (`810125cc87198a7f68eb88bd8ac959b6b77264d581c2f34c1362136ada2bfcf2`).

## 9. Decision owner + executing operator (owner-named 2026-10-26)

- Decision owner: **Balaji**. Executing operator: **Muse Spark (opencode)**.
- Powers per §6.2f: the operator may execute and abort; only the owner
  classifies results, authorizes further collection, or closes the probe
  as complete.

## Validation (executed on the completed record — all items required before signing)

- [ ] All nine items filled (no TBDs remain); owner sign-off recorded.
- [ ] Record hashed (SHA-256 over exact bytes with the hash line in its blank
  form `…recorded here: ________`).
- [ ] §6.2i checklist: v1.9+A1+A2 hierarchy acknowledged; validators green on
  the frozen basis before first collection — schema vectors 32 passed,
  signatures 52 passed, report cases 44 passed, D3 shadow suite 77 passed,
  full unit 3642 passed with 2 pre-existing baseline-rev pins, secrets suite
  14 passed, ruff check clean on touched files (7 pre-existing findings in
  untouched files), fixtures 10/10 byte-identical, probe tenant
  `iap-probe-2026-10-26-r4` verified empty/isolated read-only.
- [ ] Run set opened only after this signing commit: `d3-stagec-2026-10-26-r4`,
  fresh 48 budget, freeze object + ledger head digests recorded at opening.
