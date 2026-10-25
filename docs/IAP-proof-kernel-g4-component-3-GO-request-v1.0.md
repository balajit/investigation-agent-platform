# G4 Component 3 — Implementation GO Request v1.0

> Status: DRAFT for owner decision. Nothing in this file authorizes implementation.
> Decision requested: explicit owner GO (or GO-with-amendments, or DENY) for
> Component 3 implementation exactly as specified in
> `docs/IAP-proof-kernel-g4-component-3-design-v1.0.md` (v1.0, design-only).
> A GO here does not close G4, authorize Component 4, production wiring,
> migrations, or authority changes.

## 1. What a GO authorizes

Implementation of Component 3 only:

- New file `tests/unit/test_proof_kernel_g4_component3_properties.py` with the
  normative detector names from design D2 (P1–P6: monotonicity, refutation
  sensitivity, scope non-escalation, deterministic replay, exhaustive
  invalidation closure over the frozen 572-graph / 4-scenario enumerator,
  provenance propagation).
- Six pre-mapped mutation runs M1–M6 per the normative D3 scratch-copy
  procedure (disposable copies, trap-verified live-tree-unchanged,
  per-run journal transcriptions).
- Durable updates confined to `docs/IAP-proof-kernel-traceability-v1.0.md`
  (Component 3 section + index counts) and
  `docs/IAP_Arch_Eng_Recommendations.log` (mutation report entries).

## 2. Entry-criteria status (design §Entry criteria)

| # | Criterion | Status |
|---|---|---|
| 1 | Supplement reviewed; owner explicitly authorizes C3 implementation, including the enumerator bound (n ≤ 4, 572 graphs × 4 scenarios) and the six-mutation set (M1–M6) | **This decision** — pending |
| 2 | Component 2 formally accepted before C3 starts | **Met** — owner ACCEPTED 2026-10-26 (traceability G4 row; 4 carried residuals) |
| 3 | Mutation-to-detector mapping accepted as the pre-run contract | **This decision** — pending (mapping is design D3 table, unchanged) |

## 3. Explicit non-scope (design D7 restated — a GO does not relax these)

- No `src/` changes expected. Any `src/` change forced by a survivor is a
  contract change requiring a supplement amendment and owner re-approval
  before landing — the implementer stops and escalates instead.
- No new premise types, registry rows, rule validators, checker semantics,
  certificate or canonicalization changes.
- No database concurrency, persistence, atomicity, RLS, writer-audit, or G5 claims.
- No production wiring, rollout, migrations, or authority changes.
- No randomized property framework; no population-accuracy claims.

## 4. Worktree precondition (operator note, not a design change)

The D3 procedure records live `git status --porcelain` as the mutation-run
baseline and requires it identical afterwards. The current `g4-closure`
worktree carries uncommitted G3/G4 changes; that is tolerable under the
procedure (identical-before/after), but the operator may require those
committed or stashed first for a cleaner baseline. Owner may direct either;
the implementer records the chosen baseline in the run journal.

## 5. Exit criteria the implementation will be judged against (design §Exit criteria)

1. All P1–P6 detectors pass; enumerator 572 × 4 with zero mismatches (exact count reported).
2. M1–M6 each applied singly in scratch, detected by mapped test(s), recorded, copy deleted; live tree verified unchanged; fingerprints `--check` clean.
3. Survivors resolved via survivor protocol with owner ruling recorded.
4. Traceability Component 3 section + index counts + ledger updated; gate green except the 2 pre-existing baseline pins (reported separately).
5. No `src/`/fixture/migration/application change in the C3 diff (or an amended supplement if a survivor forced one).

## 6. Commands the gate will run (design D5)

Standalone:

```text
uv run python scripts/proof_kernel_validator_fingerprints.py --check && uv run python -m pytest -q -p no:cacheprovider tests/unit/test_composition_invalidation.py tests/unit/test_publication_decision.py tests/unit/test_g5_composition_gate.py tests/unit/test_proof_kernel_g4_component3_properties.py
```

Global gate stays BLOCKED on Component 4 (`test_proof_kernel_g4_component4_integration.py`
does not exist yet); no partial-file variant may be presented as the G4 gate.

## 7. Owner decision

- [ ] **GO** — implement Component 3 per design v1.0 (enumerator bound and M1–M6 mapping accepted as the pre-run contract).
- [ ] **GO with amendments** — (list; design versioned accordingly before implementation starts).
- [ ] **DENY** — (rationale; Component 3 stays design-only).

Signature / date: _______________
