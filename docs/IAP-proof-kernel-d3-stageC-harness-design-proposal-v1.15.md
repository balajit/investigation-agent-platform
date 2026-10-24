# D3 Stage C Harness Design Proposal v1.15

> Status: UPDATED FOR CONTROL ORDER-AGREEMENT + VECTORS v1.15 —
> owner review required for the v1.15 vectors and code changes (no registry
> change: registry v12 already carries the counterpart fields this tightening
> consumes). The v1.14 approval does not cover this revision.
> No implementation is authorized by this document: harness construction,
> Stage C execution, Phase 1, authoritative consumption, rollout, merge, or
> push each require a separate explicit owner decision.
> Planning-only artifact. This document creates no code, runs no collection,
> executes no Stage C runs, and authorizes no harness build, Stage C
> execution, Phase 1, authoritative consumption, production rollout, merge,
> or push. Each requires a separate explicit owner decision.
>
> Controlling protocol: `docs/IAP-proof-kernel-d3-phase0-approval-package-v1.9.md`
> (§6.1 taxonomy/floor, §6.2 probe + §6.2i freeze, §6.3 collection/stop,
> §6.4 interpretation, §6.5 harness contract, §1.4 authoritative join,
> §1.6 capture surface), as amended by
> `docs/IAP-proof-kernel-d3-phase0-amendment-A1.md` (SHA-256 `c96ea6c8d5a97f7d75d664d4f1e109840c7a2b2316805dbbd2f629f6d4d7302f,
> owner-approved 2026-10-24). Hierarchy: v1.9 + Amendment A1 > v1.9 alone.
> A freeze may bind these resolved rules; it cannot override them.
>
> Lineage: v1.0 → v1.1 → v1.2 → v1.2-full → v1.3 → v1.4 → v1.5 → v1.6 → v1.7 → v1.8 → v1.9 → v1.10 → v1.11 → v1.12 → v1.13 → v1.14 (all superseded; prior revision files removed in docs cleanup, history summarized in Appendix B) → v1.15 (this file), which closes
> the control order-agreement gap: both `control_order` values must agree, not
> merely be present. Only this version is normative.
>
> Companion registry: `docs/IAP-proof-kernel-d3-stageC-harness-schemas-v1.13.json`
> (registry `$id` `iap:d3-stagec:registry/v12`). The sole measured identity
> (digest + size) is stated once in Appendix A; the header carries no digest
> copy. Appendix A documents canonical bytes and vector requirements.

## 0. Decision and owner gates (controlling)

Unchanged from v1.14 except: the v1.14 approval does not cover the v1.15
vectors/code deltas below (`compare_pair` and both mirrors now require the
two `control_order` values to agree; one new negative report case). Owner
review of this revision is required before any build gates consume it.

## 1. Report-to-freeze binding (normative; §4 + §7)

`verify_report` and the semantic checker take the case freeze as a trust
input, schema-gate it (`freeze`), recompute its self-hash, and require
`report["freeze_hash"]` to equal exactly that digest with a matching
`run_set_id`. A report recomputed perfectly from source records but bound to
a different freeze voids. Rationale: the freeze validators bind authorization
records to the freeze, but nothing previously bound the report to it — the
whole R1–R8 recompute could certify a report for the wrong frozen inputs.

## 2. Run-set creation edge (normative; §2)

`INITIALIZE` is the creation edge: a run set comes into existence as
`PREPARED`. Its from-set is empty — no existing state, including `PREPARED`,
may take it — so creation happens outside the machine and every live run set
starts at `PREPARED`. This closes the enum/table gap (registry
`transitionName` named `INITIALIZE`; the transition table started at
`PREPARED`) without inventing a pre-creation run state.

Crash recovery and persistence (§2.1: temp files, fsync, atomic rename, CAS on
`state_version`, replay from anchored bytes) and the fault-injection table
(§2.2) remain unimplemented in the in-memory ledger and are explicitly out of
scope for this work package; `reservation_seq` replay covers re-submission
idempotency, not restart recovery. That persistence package is separately
scoped.

## 3. Closed-void inputs envelope (normative; §4)

The inputs envelope has no registry schema, so `verify_report` gates it
explicitly before any indexing: inputs must be an object; `turns` and
`attempts` must be present (`turns` an array, `attempts` an object);
`receipts`/`controls`/`capture_events`/`cohort_fixtures`, when present, must be
arrays; the report must be an object. Every violation raises
`HarnessValidationError` naming the missing block — never a raw `KeyError`.
Inner record shapes stay schema-gated by their own `check_record` calls
(those already raised `HarnessValidationError` before any indexing).

## 4. Receipt chain (no change; closed in v1.12)

R3-03 chain verification stays as specified: closed input schema requires
`previous_digest`; null-iff-genesis, consecutive orders, and every
non-genesis link equal to the predecessor's `payload_digest`, checked across
all receipts (referenced or not) by both the package verifier and the
semantic gate. The duplicate verifier invocation in the package
implementation is removed (single call, fail-fast after schema gating).

## 5. Low batch (v1.13, unchanged)

- The cell tuple lives in `registry.py` (mirroring the registry `cell` enum,
  covered by a parity test); `metrics.py` and `ledger.py` import it from
  there — no module depends upward for a vocabulary constant.
- The CLI registry-identity check is inline at its two call sites; the
  `_context` indirection is removed.
- `parse_instant` accepts only uppercase `Z` (RFC 3339); lowercase `z` is
  rejected.
- The `freeze.py` module docstring states the boundary order (schema
  checks first, digests derived from the validated freeze).

## 6. Control-counterpart recompute (normative; §9)

`control_match` no longer accepts a record labeled `MATCHED` on truthy
`counterpart_ref`/`control_order` fields alone. Every `control` record now
carries its shadow counterpart inline (registry `controlCounterpart`: the
seven match-tuple fields plus both sides' counterpart/order evidence), and
`evaluate_fixture` recomputes the label with `compare_pair(record,
record["counterpart"])` and requires the stored `match` label to agree:
`MATCHED` on divergent or missing counterpart evidence recomputes to
`MISMATCH`/`UNMATCHED` and never contributes; only a record whose stored and
recomputed labels are both `MATCHED` yields a pair, at most one per fixture.
Order agreement (v1.15): presence on both sides is not enough — the two
`control_order` values must be equal, so a native run cannot be paired with
a shadow run from a different sequence slot; disagreement recomputes to
`MISMATCH` even when every tuple field agrees. The vectors prove it with
`RC-control-order-mismatch` (shadow order 99 against native 1).
Rationale: the tuple comparison existed in `compare_pair` but no verification
path called it, and the report inputs carried no counterpart record to compare
against — the label was trusted, not verified. The semantic gate script
recomputes identically (inline tuple comparison plus label agreement), and the
vectors prove it with `RC-control-label-tamper` (divergent tenant),
`RC-control-counterpart-missing` (null counterpart), and the
`N-control-bad-counterpart` schema negative.

Trust boundary note: both sides of the comparison arrive inside the same
reporter-provided record — the counterpart is an embedded snapshot, not an
independently resolved shadow-run artifact. The recompute therefore defeats
label tamper and single-side mutation (including order slip), but a fully
consistent forgery of both sides is outside the verifier's power by
construction. That forgery is constrained one layer up: the owner-signed
freeze commits to the control artifact digest (`control_hash` over
`control_preimage`), and the report is bound to the freeze (§1), so an
independent audit of the frozen control artifacts detects it. Resolving
counterparts against an independent shadow-run store would require a new
frozen input surface and is separately scoped, not part of this package.

## 7. Reservation-cap binding (normative; R3 + ledger)

The reconciled attempt-ledger total is bound to the freeze's
`total_reservations_max` (schema-pinned to 48): `verify_report` and the
semantic gate reject any case whose `attempts["total"]` exceeds the frozen
limit, even when every other recompute agrees, and the vectors prove it with
`RC-reservation-over-freeze-cap` (49 linked reservations, all joins
consistent). Complement in the ledger: `AttemptLedger(total_cap=...)` stays a
construction parameter so the guard is unit-testable with lower caps, but
construction with `total_cap` above the frozen 48 fails closed in
`__post_init__` — a ledger that could reserve past the freeze would let
`reserve()` succeed where the report verifier must void. Rationale: the cap
was configurable without an upper bound and the verifier reconciled totals
without ever comparing them to the frozen limit the freeze schema pins.

## 8. Vectors content pinning (normative; CLI)

Every CLI entry point verifies the vectors file digest before trusting any
vector-defined expectation: `_load_vectors` requires the file bytes to match
the pinned `VECTORS_SHA256`, exactly like the registry pin, and rejects a
modified vectors file keeping the same registry id. Any vectors change
requires a new pin and review. Rationale: the CLI checked only the vectors'
registry id while `run_all` consumed each vector's own expected outcome — a
tampered expectation file passed the only check. Covered by the digest-match
pin test, the `--all` pin test, and the tampered-vectors rejection test.

## 13. Traceability to the findings

This section traces only the current design (v1.15, registry v12) and its
live findings. Superseded fix history (SC-V13…SC-V21, v1.13–v1.14 findings)
is summarized in Appendix B and is informational only; prior proposal
revision files were removed in docs cleanup.

| Finding | Full-doc section | Gate |
|---|---|---|
| Report-to-freeze binding unwired | §§1, 4 + registry `reportCase`/`freeze` + builder + `check_d3_stagec_report.py` | FIXED in v1.13: reportCase requires `freeze`; verifier and semantic gate recompute the self-hash and require `report["freeze_hash"]` equality with run-set match; `RC-report-freeze-mismatch` and `RC-freeze-self-hash-mismatch` reject |
| Receipt-chain verifier unwired | §4 + registry `caseReceipt` | FIXED in v1.12 (SC-V21-01); v1.13 removes the duplicate invocation only, no behavior change |
| No creation transition; ledger claims §§2–3 | §2 | FIXED in v1.13: `INITIALIZE` creation edge with empty from-set; ledger scope narrowed to the in-memory machine, §2.1/§2.2 explicitly out of scope |
| verify_report trusts container shapes | §3 | FIXED in v1.13: closed-void inputs envelope; every shape failure is a `HarnessValidationError` |
| Low items (CELLS, _context, parse_instant, docstring) | §5 | FIXED in v1.13: batch applied with parity/rejection tests |
| Control matches trusted, never compared | §6 + registry `control`/`controlCounterpart` + `control.py` + builder + semantic gate | FIXED in v1.14: records carry shadow counterparts; `evaluate_fixture` recomputes via `compare_pair` and requires label agreement; `RC-control-label-tamper` and `RC-control-counterpart-missing` reject; `N-control-bad-counterpart` schema negative. TIGHTENED in v1.15: both `control_order` values must agree (presence is not enough); `RC-control-order-mismatch` rejects. Residual boundary: the counterpart is an embedded snapshot, not independently resolved evidence — fully consistent forgery is constrained by the owner-signed freeze's `control_hash` commitment plus report-to-freeze binding, auditable out of band; an independent shadow-run store is separately scoped |
| Reservation cap can be raised | §7 + R3 + `ledger.py` | FIXED in v1.14: verifier and gate bind the reconciled total to `freeze.total_reservations_max`; `AttemptLedger` construction above 48 fails closed; `RC-reservation-over-freeze-cap` rejects |
| CLI vectors are not content-pinned | §8 + `cli.py` | FIXED in v1.14: `_load_vectors` verifies `VECTORS_SHA256` before any expectation is trusted; tampered-vectors rejection test |

## Appendix A. Schema registry (normative pointer)

The sole machine-readable registry is
`docs/IAP-proof-kernel-d3-stageC-harness-schemas-v1.13.json`
(SHA-256 `b1142ed9db57625762667685a416f95c9cd5014993bb025cfa8652df16093546`, 46636 bytes; registry `$id` `iap:d3-stagec:registry/v12`).
It is JSON Schema Draft 2020-12, self-contained, local `$ref` only.
Delta from v1.13: `control` requires `counterpart` (`$ref: #/$defs/controlCounterpart`,
the seven match-tuple fields plus both sides' counterpart/order evidence);
all record consts unchanged (`freeze/v4`, `receipt/v2` with required
`previous_digest`, `owner-authorization/v5`, `runtime-attestation/v4`,
`key-list/v1`, `report/v3`); `freeze.total_reservations_max` stays pinned to
48 and is now enforced by both verifiers, not just the schema.

Validation evidence for v1.15, rerun in this working tree on 2026-10-24:
(1) `scripts/check_d3_stagec_schema_vectors.py
docs/IAP-proof-kernel-d3-stageC-harness-schemas-v1.13.json
docs/IAP-proof-kernel-d3-stageC-schema-vectors-v1.15.json` — 32 schema vectors
passed, 0 failed, `jsonschema` Draft 2020-12;
(2) `scripts/check_d3_stagec_report.py
docs/IAP-proof-kernel-d3-stageC-harness-schemas-v1.13.json
docs/IAP-proof-kernel-d3-stageC-schema-vectors-v1.15.json` — 44 report cases
passed, 0 failed (43 carried over plus `RC-control-order-mismatch`);
(3) package CLI `check-case --all` over the same pair — 44 passed, 0 failed;
(4) `tests/unit/test_harness_*.py` — 147 passed (7 files: registry, metrics,
freeze, ledger, cli, capture_control, canonical).
Vector file: `docs/IAP-proof-kernel-d3-stageC-schema-vectors-v1.15.json`
(SHA-256 `810125cc87198a7f68eb88bd8ac959b6b77264d581c2f34c1362136ada2bfcf2`, 2247601 bytes).
CLI pins that digest in `VECTORS_SHA256` and rejects any other bytes.
Builder rebuild is byte-identical across runs.

## Appendix B. Change log (v1.0 → … → v1.14 → v1.15)

v1.15 delta (this file): control order-agreement (`compare_pair` and both
mirrors require the two `control_order` values to be equal, not merely
present; `RC-control-order-mismatch` with shadow order 99; trust-boundary
note on the embedded snapshot plus the freeze-commitment audit path);
vectors v1.15 (44 report cases). No registry change: registry v12 already
carries the counterpart fields this tightening consumes. Prior history is
summarized here; the v1.0–v1.14 proposal files were removed in docs cleanup
(this file is the sole normative revision).
