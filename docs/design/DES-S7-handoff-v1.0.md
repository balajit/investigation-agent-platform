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

## 4. Price and the concrete next fetch

`S7Handoff.price` is the estimated cost of `next_fetch` in tool-call budget units: one unit per distinct retrieval action the fetch names, one for a path-only read, capped at 20; `None` means unpriced and a price is never fabricated. It is stored in the existing JSONB handoff column (no migration); rows written before the field rehydrate as unpriced.

`next_fetch` only ever holds a concrete target: a named retrieval action or file path (`names_concrete_fetch_target`, the one definition shared with the battery's G1). It is derived from the strongest unproven hypothesis's typed claim (`derive_gap_fetch`): DEFINITION and BEHAVIOR/CAUSAL claims get a `GET_CODE` of the claim's file (or `SEARCH_CODE` of its symbol), CALL_EDGE a read of the caller's call site, ABSENCE a `SEARCH_CODE` within the claimed scope, HISTORY a `GET_CODE_HISTORY` of the file. Only code-side fetches are named; a runtime fetch is never invented for a BEHAVIOR or CAUSAL claim. When no concrete target can be named, `next_fetch` stays empty and the prose directive travels in the recommended steps only.

G1 counts an abstention as a handoff only when `next_fetch` is concrete and `price` is a positive number, or when an elimination record exists.

