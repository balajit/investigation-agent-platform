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

## 5. Rebuild notes (W1-A 2026-10-31, advisory layer removed)

Advisory emission/ranking deleted; `frontier.py`/`work_orders.py` retained. Lessons for any future consumer: cost must come from the fetch (windowed read size, search breadth) or ranking is novelty plus a hash; novelty and any damping must read one baseline function (turn entries, F2 view, last 10); an unlocatable order (REQUERY and GAP orders for ABSENCE and BEHAVIOR carry no file, span or revision) cannot be ranked and needs a fallback; seed identity for a symbol must carry the symbol, not the action name; ids and timestamps must be deterministic (uuid5, no wall clock) because the code runs in workflow context; a dispatched order must leave the ranked pool.
