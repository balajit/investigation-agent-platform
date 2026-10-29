# ADR-BOUNDARY-001: Tenant-repo widening boundary

- Status: ACCEPTED
- Motivates: REQ-investigation-loop-reengineering-v1.0 (C1, G1)

## Context

Spreading focus naturally walks upstream: the caller of the caller, the service behind the changed response. Without a boundary rule, widening has no closed frontier and the stopping guarantee fails.

## Decision

Spread stops at the tenant's declared repositories. A cause located beyond (external service, undeclared upstream) terminates as a **named handoff**: the localizing evidence (which call site, which assumed shape, which observed violation) plus the identity of the upstream — never a fetch across the boundary, never a bare abstention. The widening frontier is therefore always closed, so frontier-exhaustion is always reachable.

## Consequences

Positive: bounded, auditable investigations; onboarding can state exactly which services are in-scope. Negative: genuinely external root causes resolve to handoffs, not proofs — by design, since proof without access would be fabrication.
