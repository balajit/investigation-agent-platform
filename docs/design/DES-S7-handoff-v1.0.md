# DES-S7-handoff v1.0

- Status: ACCEPTED
- Satisfies: REQ-investigation-loop-reengineering-v1.0 (S7, G1)
- Implements: IAP-investigation-architecture-v2.0 (feedback edges)

## 1. Terminal states

Proved (possibly by contrast, with elimination record of beaten rivals); budget-deferred (single highest-value next fetch given the failure character, priced); frontier-exhausted (elimination record: what was checked, what held); cross-boundary handoff (upstream identity + localizing evidence); telemetry-deferred (resume pointer at the blocked fetch).

## 2. Discriminating-check construction

The next fetch is computed from the actual gap — no rival ruled out → name it; thin bank → name the window to widen; capped walk → name the cap; dead endpoint → name it. There is no path that emits generic filler: if no discriminating fetch exists, the state is frontier-exhausted with the record, not an abstention with boilerplate.

## 3. Handoff as loop edge

Budget-deferred and telemetry-deferred states are resumable work orders, not verdicts. Resume re-enters at the blocked fetch, never from scratch. Elimination records persist as evidence for subsequent sessions.
