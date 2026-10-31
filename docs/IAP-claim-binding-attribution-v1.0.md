# IAP claim-binding attribution (H1) — measured results v1.0

Status: FINAL for the head tree. The pre-T6 ("before") arm was NOT run, by owner decision (2026-10-30); the T6 before/after comparison is untested (see Pending measurement).

This document attributes battery outcomes to (a) the claim-binding version pin (`cbv1` vs `cbv3`) and (b) the T6 verdict-semantics changes (H2/H3/H4). It records measurements only. It does not recommend flipping the default pin; that decision stays with the owner.

## Method

- Battery: `scripts/tranche_b_battery.py` with `--claim-binding-version <cbv>`, six online cases (L1, L2, L3, L4, L5, L8) against a Spring service implementation indexed in a dedicated battery tenant. L9–L13 are offline-only and skipped.
- Isolation: the battery tenant was cleaned with `scripts/clean_battery.py --execute` before every repeat, so no run is served from another run's primary or cache. Each investigation's pin is read back and recorded; every graded investigation matched the requested pin.
- Repeats: 3 per head arm and pin. Intervals are 95% Wilson intervals and are wide at n=3 per case; pooled figures use n=18 case-runs per arm.
- Per-case cause is derived from the persisted conclusion, hypotheses, evidence rows by read mode, and retrieval-attempt counts for each graded investigation.

| Arm | Tree | Pin | Repeats |
|---|---|---|---|
| head | T6 included (H2/H3/H4) | cbv1 | 3 (complete) |
| head | T6 included | cbv3 | 3 (complete) |
| before | tranches 0–5, no T6 | cbv3 | not run (owner decision) |
| before | tranches 0–5, no T6 | cbv1 | not run (owner decision) |

## Head tree results

Per-case passes (of 3) with 95% intervals:

| Case | Question shape | cbv1 | cbv3 |
|---|---|---|---|
| L1 | definition location | 3/3 [44%–100%] | 3/3 [44%–100%] |
| L2 | call edge | 2/3 [21%–94%] | 2/3 [21%–94%] |
| L3 | absence plus history | 2/3 [21%–94%] | 3/3 [44%–100%] |
| L4 | location | 3/3 [44%–100%] | 3/3 [44%–100%] |
| L5 | behavior / causal | 0/3 [0%–56%] | 0/3 [0%–56%] |
| L8 | honest abstention expected | 3/3 [44%–100%] | 3/3 [44%–100%] |

Pooled case-pass: cbv1 13/18 = 72% [49%–88%]; cbv3 14/18 = 78% [55%–91%]. The intervals overlap almost entirely. At this sample size the two pins are not distinguishable.

Passes per run (of 6): cbv1 4, 5, 4; cbv3 5, 5, 4.

### Failure causes (head tree)

| Case | Runs failing | Observed cause |
|---|---|---|
| L5 | 6 of 6 (both pins) | 5–6 own evidence rows banked, one BEHAVIOR or CAUSAL hypothesis, 7–9 verify turns, hypothesis never reaches VERIFIED; ends `verification_failed_or_limit_reached`. Deterministic across pins; not a pin effect. |
| L2 | 1 of 3 (cbv1), 1 of 3 (cbv3) | cbv1: CALL_EDGE hypothesis stays under investigation after 8 own rows. cbv3: no hypothesis formulated; `defer_repair_budget_exhausted` with 0 verify turns. Different mechanisms, each seen once. |
| L3 | 1 of 3 (cbv1) | ABSENCE hypothesis under investigation with a HISTORY hypothesis only proposed; `verification_failed_or_limit_reached`. |

No failure on the head tree was caused by evidence dedup: failing cases own evidence rows and the read path unions linked rows. No case failed because cited rows evaporated or because a rival bar demoted the answer.

### G1 (bound-or-concrete-handoff)

G1 satisfied per run (of 6): cbv1 3, 4, 3; cbv3 4, 4, 3. Every G1 miss is a case whose conclusion is an abstention with a generic template `next_fetch` (L5 and L8 in every run, plus the one-off L2/L3 failures). Under the strict G1 an honest abstention scores as a miss, so L8 passes the battery grader but not G1. The structured handoff has no price field and `next_fetch` is still template text; this is the open T2 remainder and it caps G1 independently of pin choice.

## Earlier single-run signal

A single cbv3 run on the pre-restart tree gave INSUFFICIENT on L1, L2, L4 and L5, with L8 an honest abstention; L3 was served from an earlier cbv1 primary and could not be counted. That run was confounded by tree changes and a non-clean tenant. On the head tree with a clean tenant, L1 and L4 passed in 6 of 6 cbv1/cbv3 runs, so the earlier result did not reproduce.

## T6 before/after

UNTESTED. No before arm was run, so this document makes no claim about whether T6 (H2/H3/H4) changed battery outcomes under either pin. By owner decision, the comparison is deferred to the rerun described under Pending measurement.

## Confounds and limits

- There is no before arm, so no result here can be attributed to T6 or to the tranche 0–5 changes. Head-tree results are a single-tree measurement.
- n=3 per case per arm; per-case intervals span most of [0, 100%]. Only large effects are detectable.
- The LLM provider is nondeterministic; run-to-run variation inside one arm (4–5 of 6) is of the same size as the cbv1/cbv3 difference.
- Arms ran sequentially, not interleaved, so provider drift over time is confounded with tree and pin.
- The battery has six online cases, one of which (L5) fails deterministically.

## Pending measurement

The T6 before/after comparison is untested, by owner decision: the before arm was not run. This also leaves T6's effect on the default path unmeasured; the default pin is `cbv1`, the head tree scores 13/18 on it, and nothing is available to compare it against.

Task: after the outstanding fixes land, rerun the full 2x2 (before and after trees, each under cbv1 and cbv3) with a clean battery tenant per repeat and at least 3 repeats per cell, then fill the T6 comparison for both pins.

## Decisions that remain with the owner

- Whether to flip the default pin from `cbv1`. These data show no measurable pass-rate difference between pins on the head tree.
- Whether to invest in the T2 remainder (price field and concrete `next_fetch`), which determines whether abstentions can satisfy G1.
