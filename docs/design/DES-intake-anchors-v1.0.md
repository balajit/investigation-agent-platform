# DES-intake-anchors v1.0

- Status: ACCEPTED
- Satisfies: REQ-investigation-loop-reengineering-v1.0 (S1, C2)
- Implements: ADR-PLANE-001 (spread budget)

## 1. Anchors

Every investigation records onset T (when the issue started) and last-known-good G (when the area was last known healthy), each a timestamp or explicit `unknown`. Defaults are forbidden: an unanchored investigation records `T: unknown, G: unknown` visibly, and downstream window logic treats explicit-unknown differently from assumed-now (assumptions are recorded, never silent).

## 2. Derived windows

The working window [G, T] (or [T − default, T] when G is unknown, marked as assumed) scopes history walks ("complete" means covering the window), runtime retrieval, and contrast-pair selection. The record shows which bounds were given and which assumed.

## 3. Spread budget

Intake assigns a spread budget drawn from the per-investigation tool-call envelope. Each widening fetch is priced against it; exhaustion is a terminal path (budget-deferred handoff), not an error. Budget state is visible to the reasoner so work orders can weigh cost.

## 4. Versioning

Anchor semantics are recorded on the investigation version; changes to windowing rules are version-pinned per entity, never env-switched.
