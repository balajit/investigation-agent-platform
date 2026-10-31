# DES-collect-frontier v1.0

- Status: ACCEPTED
- Satisfies: REQ-investigation-loop-reengineering-v1.0 (G1, S2)
- Implements: ADR-PLANE-001 (collect plane)

## 1. Frontier

Best-first search over (service, file, commit, span, config) nodes. Each entry carries score, depth, provenance. Score = expected-information-gain per cost × novelty (one-minus-max-similarity to collected evidence). Early scores are ordinal (rank fetches), calibrated to cardinal later from fetch→outcome logs.

## 2. Spread dimensions

Keyed to S6 failure character: no rows → spread evidence layers (code exhausted, go runtime, cheapest first); rows without match → assumption spread; unbeaten rival → rival spread; stale match → time spread (widen [G, T], then beyond); local cause complete but external → named handoff (ADR-BOUNDARY-001).

## 3. Stopping rule (three exits)

Budget exhaustion (priced handoff), fixpoint (frontier empty or all scores below threshold), diminishing returns (top marginal gain below threshold for k rounds), or confidence with corroboration (posterior above threshold supported by ≥2 independent artifact types). Depth/TTL caps bound call-graph and history-walk radius.

## 4. Loop discipline

Collection appends to the evidence lake with provenance; never rewrites. Work orders from the reasoner seed the frontier; distill usage signals reweight scoring. Long-running: days/weeks acceptable on the cheap plane.

## 5. Rebuild notes (W1-A 2026-10-31, advisory consumer removed)

`order_candidates`/`FrontierCandidate` retained with no production caller. Any future smallest-fetch consumer must price cost monotonically in fetch size (clamped, never raising), read one collected baseline, fall back to kind order for unlocatable orders, and carry seed identity with the symbol. See `Sonnet_W_items_detailed_design.md` D7 for the full consumer sketch and why it was not wired (no live forensic of an ordering failure; invented costs; at most 3 of 5 kinds locatable).
