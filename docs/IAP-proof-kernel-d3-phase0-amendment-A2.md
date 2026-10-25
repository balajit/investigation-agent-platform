# D3 Phase 0 Protocol Amendment A2 (OWNER-APPROVED Normative Delta to v1.9+A1)

> Status: OWNER-APPROVED 2026-10-26. Planning artifact; authorizes no
> implementation, Stage C execution, Phase 1, authoritative consumption,
> rollout, merge, or push.
>
> Base protocol: `docs/IAP-proof-kernel-d3-phase0-approval-package-v1.9.md`
> (SHA-256 `547aee50446c0fcc674a67e5f239e15de6dcf5f73de52474f3f133ce2be965c3`)
> plus Amendment A1 (`c96ea6c8d5a97f7d75d664d4f1e109840c7a2b2316805dbbd2f629f6d4d7302f`).
> Both base files are preserved byte-identical; this amendment is the
> controlling delta where it conflicts with the base. Hierarchy:
> v1.9 + A1 + A2 > v1.9 + A1 > v1.9 alone. A freeze may bind these resolved
> rules; it cannot override them.
>
> Motivation: the development runtime now has one active verifier behavior
> (historical `v1`/`v2`/`v3` labels are audit-only and select no runtime
> branch; new investigations create no verifier pins). The frozen v1.9 rule
> (`verifier_version == v2`, §§0.2/2.1, A-1, §6.2b) cannot govern
> post-consolidation formal collection without redefinition. This amendment
> defines that mapping. It does not reinterpret freeze `ff0cf8e7…` or run set
> `d3-stagec-2026-10-24-r2` (verdict `INSUFFICIENT, kept open` stands).

## A2-1. Eligibility redefined for post-consolidation run sets (OD-03)

v1.9 §0.2 ("eligible turn ... `verifier_version == v2`, on a
definition-claim candidate") and §6.2b ("evaluates at least one
definition-claim candidate at the verify gate under
`verifier_version == v2`") are superseded for run sets collected under a
freeze that binds this amendment, as follows:

"An eligible turn is a turn with a recorded native outcome AND a recorded
typed observation under the frozen comparison rule, with
`active_behavior_version == <frozen-behavior-id>` and
`protocol_version == <frozen-protocol-id>` (both pinned in the freeze record),
on a definition-claim candidate. An eligible run completes without platform
error AND evaluates at least one such candidate at the verify gate AND
records a native outcome for it. Runs completing with zero such candidates
are non-eligible runs: counted separately, reported, excluded from the
denominator — never retroactively reclassified. All other eligibility,
exclusion, void, and stop rules (§§6.1–6.3, A1 OD-01/OD-02/BUD-01) are
unchanged."

The frozen behavior id and protocol id are named in the freeze record (e.g.
behavior `unified-dev-v1`, protocol `v1.9+A1+A2`); the harness validates both
before any join. `verifier_version` labels on old rows are audit metadata
only and satisfy no eligibility clause by themselves.

## A2-2. Pre-consolidation rows excluded by default (OD-04)

Rows and observations recorded before the consolidation (any recorded
`verifier_version` pin of `v1`/`v2`/`v3`, or unmarked rows that ran the
then-current path) are excluded from the denominator of any run set bound to
this amendment, unless the owner explicitly re-baselines a named row set in
the freeze record with a stated migration (row ids, original semantics,
re-validation evidence). Default is exclusion, never silent reinterpretation.
The r1/r2 corpora (6 + 12 probe rows retained as evidence) remain under their
original `v2`-only rule and are inadmissible in the new set, never merged.

## A2-3. Observation tagging, join keys, and telemetry under unified behavior

- Observations are tagged with `active_behavior_version` (the single dev
  behavior) and the recorded `verifier_version` label is retained alongside
  for audit only. Unknown behavior or protocol ids fail closed (`Unknown`
  / `Unavailable` paths preserved; corrupted metadata is never guessed).
- `VerificationEngine(verifier_version=...)` selects no branch; the argument
  is API-compatibility only. `resolve_verifier_version` source vocabulary
  (`unified-dev` / `unmarked`) is telemetry, not eligibility.
- Receipt-time pairing (envelope + `envelope_digest`), producing-path labels,
  `mirror_notes` vs `capture_errors` separation, N21 empty-trace absence, and
  the T3 conclude-gate receipt chain are unchanged. The envelope carries the
  bound amendment id so analysis joins only same-protocol observations.
- No new telemetry keys beyond the §1.6 allowlist; any addition requires a
  further amendment.

## A2-4. Carry-overs unchanged (BUD-02)

Controlling hierarchy v1.9 + A1 plus this A2; harness proposal v1.15,
registry `iap:d3-stagec:registry/v12`, vectors v1.15, report checker,
receipt-chain, `CONTROL_MATCH` recompute (both `control_order` values equal),
48-reservation ceiling, vectors pinning, and §§6.2e–6.2f retry/void/replacement
semantics are unchanged. A future v1.10 consolidation may merge v1.9 + A1 +
A2 into a single file; until then the three jointly constitute the
controlling protocol.

## Approval record

| Field | Value |
|---|---|
| Decision ID | DEC-2026-10-26-A2 (covers OD-03, OD-04, BUD-02) |
| Approver | Owner |
| Date | 2026-10-26 |
| Base protocol | v1.9 `547aee50…` + A1 `c96ea6c8…` (byte-identical) |
| Effect | Normative amendment to §§0.2/6.2b eligibility, pre-consolidation row handling, unified-behavior tagging; carry-overs unchanged |
| Freeze binding | A freeze binding A2 must pin behavior id + protocol id; r2 freeze `ff0cf8e7…` does not bind A2 |
