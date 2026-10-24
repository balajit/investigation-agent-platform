# D3 Phase 0 Protocol Amendment A1 (Owner-Approved Normative Delta to v1.9)

> Status: OWNER-APPROVED 2026-10-24. Planning artifact; authorizes no
> implementation, Stage C execution, Phase 1, authoritative consumption,
> rollout, merge, or push.
>
> Base protocol: `docs/IAP-proof-kernel-d3-phase0-approval-package-v1.9.md`
> (SHA-256 `547aee50446c0fcc674a67e5f239e15de6dcf5f73de52474f3f133ce2be965c3`).
> The base file is preserved byte-identical; this amendment is the
> controlling delta where it conflicts with the base. Hierarchy:
> v1.9 + Amendment A1 > v1.9 alone. A freeze may bind these resolved rules;
> it cannot override them.

## A1-1. v1.9 §6.5 item 7 amended — timeout disposition (OD-01)

"An UNRECORDED timeout — or any timeout that prevents the outcome from being
recorded, corrupts ordering/identity/persistence/the frozen environment, or
whose containment cannot be proven from the record — voids the affected runs
(retain artifacts, mark INSUFFICIENT EVIDENCE, never trim/combine/rerun under
that identity; replacement only by new freeze, new run-set ID, newly
authorized budget). A RECORDED turn-contained timeout whose error outcome
arrived through the normal channel with `producing_path` and a joinable
envelope is an excluded attempt per §6.1 (budget consumed, collection
continues)."

## A1-2. v1.9 §6.5 item 4 amended — superseded-attempt disposition (OD-02)

"A superseded-attempt observation that is merely present is EXCLUDED from the
denominator and counted separately per §1.4, never joined, and does not void
by presence. An attempted cross-execution join of a superseded observation to
another execution's native result, or inability to prove accepted-attempt
identity, is an ambiguous join and voids the run set."

## A1-3. v1.9 §6.2e cap semantics clarified — reservation buckets (BUD-01)

The "12 attempted runs per decision cell" cap applies to pre-run
reservations against owner-frozen expected-cell buckets, with a hard
run-set-total limit of 48 reservations. Each attempt is charged exactly once
to its frozen expected-cell bucket at reservation time. An attempt whose
observed outcome maps to a different cell is recorded as EXCLUDED against the
reserved bucket (reason `BUDGET_RESERVED_CELL_MISMATCH`) and is never
re-attributed, refunded, or transferred. Observed-cell counts are reported
separately as diagnostic data and are not subject to the 12-cap; no parity,
coverage, or pass claim may be derived from exceeding observed counts.
Post-hoc reassignment, `UNKNOWN_CELL` pools, target-cell guessing, and
refunds are forbidden. This clarification changes cap semantics versus a
strict observed-cell reading and is adopted here by explicit owner decision.

## Approval record

| Field | Value |
|---|---|
| Decision ID | DEC-2026-10-24-A1 (covers OD-01, OD-02, BUD-01) |
| Approver | Owner |
| Date | 2026-10-24 |
| Base protocol | v1.9, SHA-256 `547aee50446c0fcc674a67e5f239e15de6dcf5f73de52474f3f133ce2be965c3` |
| Effect | Normative amendment to v1.9 §§6.5 items 7/4 and §6.2e cap semantics |
| Design confirmations recorded alongside (not protocol changes) | DEC-2026-10-24-D01 (D-01 6-tuple denominator), DEC-2026-10-24-DIGEST (v1.9 §2.1 envelope formula normative), DEC-2026-10-24-CAP (frozen artifact bundle as Layer-1/resident-byte source), DEC-2026-10-24-ID (signed ownerAuthorization + wrapper manifest trust structure) — wordings in the Stage C harness proposal §12.1 |

A future v1.10 consolidation of the base package may merge this delta into a
single file; until then, this amendment plus the base file jointly constitute
the controlling protocol.
