# ADR-PROOF-001: Assumption-contrast and delta binding over subject matching

- Status: ACCEPTED
- Motivates: REQ-investigation-loop-reengineering-v1.0 (S5, S6, G1)

## Context

Dormant bugs (flag-gated, data-gated, contract drift, regime crossing) break an assumption that held at last-known-good and fails at onset. No single row establishes this; only a contrast across time does. The previous history rule bound claims to commit-subject text — certifying agreement between two derived texts while the code sat unconsulted — and commit subjects are annotations of occasion, not observations of content.

## Decision

Two proof shapes replace subject matching:

1. **Delta binding**: a change claim verifies iff the claimed change appears in the banked diff/statistics for the target path at the cited revision. The commit hash is a pointer, the diff is the witness, the subject is steering only.
2. **Assumption-contrast**: a dormant-cause claim verifies on an old-behavior/new-behavior row pair plus the code row encoding the assumption (old traces passing + current traces failing + the branch that assumed the old shape).

Both ride recorded claim-binding versions; unmarked rows keep current semantics.

## Consequences

Positive: verifiability tracks repository contents instead of message literary quality; vacuous messages verify fine when the diff supports the claim. Negative: requires hydrated diffs at verify time and old-behavior rows whose retention must be specified per tenant.
