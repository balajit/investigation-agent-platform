# DES-reason-workorders v1.0

- Status: ACCEPTED
- Satisfies: REQ-investigation-loop-reengineering-v1.0 (G1, S7)
- Implements: ADR-PLANE-001 (reason plane)

## 1. Brief-only sessions

The reasoner reads only the evidence brief plus content pointers for drill-down. Full-lake access from the reasoner is not permitted: it couples recall to token spend and reintroduces the failure mode this architecture removes.

## 2. Outputs

Verdicts with cited bindings, or work orders: hypotheses plus discriminating next fetches fed back to the collection frontier as seeds. Work orders carry expected value (which failure character they address) and cost estimates.

## 3. Escalation

A cheap investigator model drafts; the frontier model engages only below confidence threshold or above stakes threshold. Thresholds calibrated on labeled replays (cost-vs-quality Pareto); corroboration required for high-stakes claims; audit-escalation samples guard against confidently-wrong cheap tiers; recalibration on workload shift.

## 4. Blind-spot discipline

Cheap and expensive tiers must be diverse (rules + retrieval + small model, not same-family prompts) so escalation headroom is real, not correlated error.
