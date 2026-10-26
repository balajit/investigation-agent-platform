# G4 Disposition Package v1.0 — DRAFT (not accepted)

Status: DECIDED 2026-10-26 — option (b) PARTIAL with direction (owner session decision). This file is now the record; direction quoted verbatim in §5.

## 1. What is being decided

Global G4 disposition (G4 direction): whether the proof-kernel work recorded below is accepted as the G4 exit basis, with residuals carried explicitly, and what (if anything) routes to G5/G6/G7. Components 1–4 are each implemented; C2 is accepted, C1/C3/C4 await no further component-level action — the global decision is recorded in §5 (PARTIAL with direction, 2026-10-26).

## 2. Evidence (observed, green)

Authoritative gate command (parent `docs/IAP-proof-kernel-g4-phase-design-v1.1.md`, criterion 6):

```text
uv run python scripts/proof_kernel_validator_fingerprints.py --check && uv run python -m pytest -q -p no:cacheprovider tests/unit/test_canon_identity.py tests/unit/test_manifest_evaluation.py tests/unit/test_typed_rules.py tests/unit/test_certificate_checker.py tests/unit/test_composition_invalidation.py tests/unit/test_publication_decision.py tests/unit/test_g5_composition_gate.py tests/unit/test_proof_kernel_fixture_integrity.py tests/unit/test_proof_kernel_validator_fingerprints.py tests/unit/test_proof_kernel_g4_component1_evidence.py tests/unit/test_proof_kernel_g4_component2_rules.py tests/unit/test_proof_kernel_g4_component3_properties.py tests/unit/test_proof_kernel_g4_component4_integration.py tests/unit/test_proof_kernel_premises.py
```

Observed 2026-10-26: fingerprints clean (6 modules verified); per-file tallies below sum to `290 passed, 1 skipped` (the skip is the pre-existing G5 live-DB operator leg). When a D3-family fix lands, verification follows `docs/IAP-proof-kernel-g4-tracka-retest-procedure-v1.5.md` (Track A).

| Suite file | Observed |
|---|---|
| test_canon_identity.py | 20 passed |
| test_manifest_evaluation.py | 14 passed |
| test_typed_rules.py | 11 passed |
| test_certificate_checker.py | 27 passed |
| test_composition_invalidation.py | 18 passed |
| test_publication_decision.py | 11 passed |
| test_g5_composition_gate.py | 6 passed, 1 skipped |
| test_proof_kernel_fixture_integrity.py | 14 passed |
| test_proof_kernel_validator_fingerprints.py | 4 passed |
| test_proof_kernel_g4_component1_evidence.py | 5 passed |
| test_proof_kernel_g4_component2_rules.py | 76 passed |
| test_proof_kernel_g4_component3_properties.py | 39 passed |
| test_proof_kernel_g4_component4_integration.py | 20 passed |
| test_proof_kernel_premises.py | 25 passed |

Full unit suite observed 2026-10-26: `3576 passed, 4 skipped, 6 xfailed`, with only the 2 pre-existing baseline revision-pin failures (`test_proof_kernel_baseline.py`, out of scope, unchanged).

Component record:

- C1: fixture-level exit signed by owner review 2026-10-24 (structural + integrity per traceability §Component 1 Exit Record).
- C2: implemented under 2026-10-24 conditional GO, ACCEPTED by owner 2026-10-26 (76-rule-suite vectors plus migrated kernel suites, all green in the gate above).
- C3: implemented under 2026-10-26 C3 GO — 39 property tests green; standalone `74 passed, 1 skipped`; mutations M1–M6 all DETECTED (mapping notes R-C3-01–R-C3-04, no survivors).
- C4: implemented under 2026-10-26 C4 GO per design v1.3 — 20/20 green; `C4_CASES` 19 entries with observed digests (single source in-module); D3 scan zero out-of-allowlist; zero residuals. Review clarification recorded: valid-row reason text is pinned against a direct call to the checker-dispatched semantic function (checker VALID still asserted; the text is not returned by `check_certificate`); C4's no-source-change claim is change-attribution for C4's own change set only.

## 3. Residuals carried (explicit, none silent)

- C1-R1: Component 1 `content_hash` values are symbolic placeholders by fixture-envelope design; digest-to-byte binding proven by C2 vectors; no gate relies on C1 placeholder digests.
- C2-R1: real-world BUILD_SOURCE_MAPPING authority remains OPEN (test-only synthetic authority approved).
- C2-R2: CALL_EDGE producer/checker divergence remains documented; producer tightening requires a separate C2 amendment decision.
- C2-R3: premises suite runs in the gate; parent list revision rides with the C3 supplement.
- C2-R4: the two pre-existing baseline revision-pin failures and mypy issues in untouched files remain out of scope.
- C3 R-C3-01–R-C3-04: mutation-mapping notes (M2 re-point at direct-semantics detectors with redundant K11 as defense-in-depth; M3 checker-side names hold by design; M5 failed-search not admitted; M6 decision-level denial is a separate layer). Plus recorded producer scope-visibility gap for definition/call_edge (checker-K11-only sensitivity, same family as C2-R2).
- C4: none.

## 4. Out of scope — G5-owned (no G4 claim)

Publication-record insert, hypothesis/finding/conclusion status updates, database atomicity, interleaving, live-snapshot binding, CAS/version-guard behavior, writer routing (`single`/`all`/`any`), RLS session handling, contradiction listing, the grandfathered `publication_commit_service.py` path, and any production behavior. Exact footnote carried from the C4 report contract:

> G4 pure-decision coverage only: this report pins the in-memory certificate-to-decision path. It claims no publication-record insert, no status update, no atomicity/interleaving behavior, no writer routing, and no production behavior — all owned by G5.

## 5. Decision (owner — blank)

- [ ] (a) G4 PASS, scoped to §2 evidence with §3 residuals carried and §4 owned by G5.
- [x] (b) G4 PARTIAL, with direction (owner verbatim): "Partial - progress to the next steps - Kept in partial as there are changes happening to fix D3 families need retesting."
- [ ] (c) DEFER (state what is missing): _______________.

Owner signature / date: owner session decision, 2026-10-26.

## 8. Follow-on procedure versions (appended post-decision; §§1–7 unchanged)

- Track A verification follows `docs/IAP-proof-kernel-g4-tracka-retest-procedure-v1.5.md` (single final record; superseded versions removed). The §2 pointer above is read as this procedure.
- G5 atomicity completion is proposed in `docs/IAP-proof-kernel-g5-atomicity-proposal-v1.3.md` (DRAFT, single final record; superseded versions removed; extends the existing 12-test live module with T13–T16 rather than creating a parallel module; T15/T16 pre-registered as likely-red lifecycle-denial vectors per the post-write finding).
