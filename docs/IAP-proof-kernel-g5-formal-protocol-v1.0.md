# G5-Formal Operator Protocol v1.0 — Live-DB Interleave + Commit-Path Role Review

> Status: operator runbook, not implementation authorization. Execution
> requires a staging database (production clone or equivalent volume);
> never run Part A against production. Precondition: G5-unit green
> (`tests/unit/test_g5_composition_gate.py`). Exit: G5 PASS declared by
> the gate owner, or HOLD with named gaps. This protocol tests what
> exists: the hypothesis CAS save path (`SqlAlchemyHypothesisRepository.save`).
> The pure publication decision (`decide_publication` / `build_commit`)
> has no persisted commit row yet — there is no DB publication record to
> interleave; Part A scopes honestly to the premise-write path and §4
> records the residual gap.

## 0. What is (and is not) under test

- Under test: concurrent premise writes against `hypotheses` lose
  exactly one writer via the single conditional `UPDATE ... WHERE
  version = N` (`hypothesis_repository.py:118`), rowcount-decides, loser
  gets `ConcurrencyError` (deny/reevaluate, never a mixed snapshot);
  RLS tenant isolation holds throughout; no silent overwrites.
- Not under test: a persisted publication record (does not exist);
  cross-table premise-vs-certificate read consistency beyond the
  single-row atomicity the schema provides (§4 residual).

## 1. Preconditions (operator verifies before Part A)

1. Staging DB at migration head `032_hypothesis_version`, seeded with
   pre-032-style rows (version backfilled to 1) plus fresh rows.
2. App connects with the **application role** (not superuser, not table
   owner session): `SELECT rolbypassrls FROM pg_roles WHERE rolname = '<app>'`
   must return false for the app role.
3. `FORCE ROW LEVEL SECURITY` confirmed on `hypotheses`:
   `SELECT relforcerowsecurity FROM pg_class WHERE relname = 'hypotheses'`
   must be true (set by `001_add_rls.py`).
4. Single-writer invariant holds: the only `UPDATE`/`INSERT` on
   `hypotheses` is `hypothesis_repository.py:118` / `:154`
   (re-grep before running; any new writer voids this protocol).

## 2. Part A — controlled interleave (live staging DB)

Use one tenant `T`, one investigation `I`, one hypothesis `H` at known
version `N` (read it first). Run each case; record rowcounts and outcomes.

### Case 1 — loser denied on concurrent premise change (raw SQL, overlapping xacts)

Session A and B, both: `BEGIN; SET LOCAL app.tenant_id = 'T';`

1. A: `SELECT version FROM hypotheses WHERE id = '<H>'` → N.
   B: same → N.
2. A: `UPDATE hypotheses SET statement = 'premise-A', version = N + 1
   WHERE id = '<H>' AND tenant_id = 'T' AND version = N;` → rowcount 1.
   (Hold A's transaction open: do not commit yet.)
3. B: same UPDATE with `statement = 'premise-B'` → **blocks** on the row lock.
4. A: `COMMIT;`
5. B: unblocks → **rowcount 0** (predicate no longer matches).
   B must treat 0 as denial: `ROLLBACK`, surface stale/reevaluate —
   never a second write, never a retry that overwrites A's row.

PASS BAR: exactly one winner; loser rowcount 0 + rollback; final row
reads exactly the winner's full column set (single-statement atomicity —
no column from the loser).

### Case 2 — app-level concurrent `save()` (lost update becomes `ConcurrencyError`)

Two coroutines, same `expected_version = N`, distinct payloads, same
`(T, I, H)`, launched concurrently against the app repository:

- Exactly one returns normally (row at N+1 with the winner's payload).
- Exactly one raises `ConcurrencyError`; its transaction is rolled back
  by `rls_session` (F-031) — verify the pooled connection is reusable
  and the loser's payload appears nowhere in the row.
- Repeat ×50; tally must be 50/50 split-free: 50 wins total across both
  sides, 50 `ConcurrencyError`s, 0 silent overwrites, 0 mixed rows.

### Case 3 — cross-tenant write rejected

Session with `app.tenant_id = 'OTHER'`: `UPDATE hypotheses ... WHERE
id = '<H>' AND tenant_id = 'OTHER' AND version = N` → rowcount 0.
App-level: `save(tenant_id='OTHER', ..., expected_version=N)` →
`ConcurrencyError`. No row touched (verify `H` unchanged under `T`).

### Case 4 — concurrent inserts, same id

Two concurrent `save(..., expected_version=0)` with the same new id:
exactly one inserts (version 1); exactly one gets `ConcurrencyError`
("concurrent insert won"). No duplicate row, no version-0 row persists.

### Case 5 — no mixed snapshot for readers

1. Open reader R1 (`REPEATABLE READ`, tenant `T`), `SELECT *` on `H` → vN.
2. Run Case 1 (A wins, vN+1 committed).
3. R1 re-reads → still vN (snapshot isolation, expected and consistent).
4. New reader R2 → full vN+1 row; assert every premise column equals the
   winner's payload (no loser column leaks — structural from single-row
   atomicity, verified by full-row comparison, not assumed).

## 3. Part B — migration + role review of the commit path

### B1 — migration 032 on staging data

1. Restore a pre-032 backup to staging; confirm `version` column absent.
2. `alembic upgrade head`: column appears, all existing rows read
   `version = 1` (server_default backfill).
3. App read of a backfilled row → `Hypothesis.version == 1`
   (`_from_orm` getattr default covers even a NULL read).
4. First guarded write with `expected_version=1` → row at 2, no
   `ConcurrencyError`. (A write with `expected_version=0` on an existing
   id must raise, not overwrite — covered by Case 4 logic in reverse.)
5. `alembic downgrade -1 && alembic upgrade head` round-trips cleanly.

### B2 — role review (record outputs in the verdict)

1. App role: `rolbypassrls = false`, `rolsuper = false`.
2. `tenant_isolation` policy present on `hypotheses` (+ the other five
   tables from `001_add_rls.py`); `relforcerowsecurity = true` on each.
3. Confirm the policy predicate keys on `app.tenant_id` (the exact GUC
   `rls_session` sets) — a predicate on any other setting voids RLS.
4. Confirm every `hypotheses` access in
   `hypothesis_repository.py` (1 writer + 3 readers) goes through
   `rls_session`; any direct `factory()` session is a finding.
5. Confirm no second writer exists (re-run the §1.4 grep; include
   raw-SQL `UPDATE hypotheses` / `INSERT INTO hypotheses` string search
   across `src/`).

## 4. Known residual (not closable by this protocol)

There is no persisted publication row: `PublicationCommit` is an
in-memory binding. A future persisted commit record needs its own
linearization (commit conditional on premise version, same row or
same-transaction check) and its own interleave protocol. Until then,
cross-table premise-vs-certificate read consistency rests on
single-row atomicity + reader version checks, and any authoritative
rollout decision must name this gap explicitly.

## 5. Verdict block (operator fills)

```text
G5-formal verdict: PASS / HOLD
Date / operator / staging snapshot id:
Cases 1–5: (per-case PASS/FAIL + rowcount logs attached)
B1 migration: PASS/FAIL   B2 roles: PASS/FAIL (outputs attached)
Gaps (if HOLD):
```

PASS requires every case green, B1/B2 clean, and §4 acknowledged in the
rollout decision. Any mixed snapshot, silent overwrite, RLS bypass, or
second writer is an automatic HOLD and a stop-condition finding.
