# DES-S6-binding v1.0

- Status: ACCEPTED
- Satisfies: REQ-investigation-loop-reengineering-v1.0 (S5, S6, G3)
- Implements: ADR-PROOF-001; IAP-investigation-architecture-v2.0 (stage map)

## 1. Binding checks

- Range: claimed region present in a cited pinned row at the cited revision.
- Delta: claimed change present in the hydrated diff/statistics for the target path at the cited revision (subject text is steering only).
- Contrast: old half holds at/before G, new half holds at/after T, code row encodes the broken assumption.
- Mechanism-shift: metric conditional on architecture-derived parents shifts across T while parents hold (where tenant telemetry supports it).

## 2. History verdict semantics

ANSWERED passes only on verifier confirmation of cited rows (with recency proof where "most recent" is claimed — commit-ordered evidence plus declared walk completeness; capped or incomplete sets abstain on recency). Proven-absence INSUFFICIENT (complete empty-history proof banked) passes. Unbound INSUFFICIENT routes into the spread policy, never into a stop. S4 failure keeps S5/S6 not-reached.

## 3. Rival bar

Causal conclusions name the strongest rival considered and the evidence ruling it out. No named rival, no causal verdict.

## 4. Versioning

New binding rides a recorded claim-binding version; unmarked rows keep current semantics. The S6 stage gate gains a shape branch (history vs code/runtime) so history verdicts are checked, never auto-passed.
