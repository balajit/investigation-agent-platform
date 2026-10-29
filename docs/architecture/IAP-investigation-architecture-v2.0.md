# IAP investigation architecture v2.0

- Status: ACCEPTED
- Implements: REQ-investigation-loop-reengineering-v1.0; ADR-PLANE-001, ADR-BOUNDARY-001, ADR-PROOF-001.

## 1. Planes

Collection (cheap, to fixpoint) → distillation (recall valve) → reasoning (rare, brief-only, emits work orders back to collection). Escalation-gated, calibrated on labeled replays. See ADR-PLANE-001.

## 2. Stage map (S1–S7)

S1 records onset T, last-known-good G (or explicit unknown), and a spread budget. S2 generates mechanism hypotheses plus the four dormant-activation templates, each naming its discriminating fetch. S3 surfaces authorization/availability gaps visibly. S4 qualifies evidence per layer (code: four pinned fields; history: type + path + revision + scope relevance, or complete-empty proof; runtime: window pinning; contrast pairs jointly). S5 requires named rivals ruled out with evidence. S6 binds via range, delta, contrast, or mechanism-shift checks — never auto-passes. S7 emits the priced handoff: proved, budget-deferred with next fetch, frontier-exhausted with elimination record, or named cross-boundary handoff.

## 3. Evidence hierarchy

Runtime rows outrank code rows outrank history rows outrank messages. Every binding rule prefers the artifact closest to execution; each plane declares its data dependencies, and missing data is a visible gap shaping the handoff.

## 4. Feedback edges

Verdict → work order → collection frontier (spread on failure character); distill usage signals (which brief claims were used/unused/wrong) → collector/distiller scoring; endpoint failures → BLOCKED_ON_TELEMETRY → DEFERRED with resume pointer, logged at ERROR.

## 5. What v2.0 changes from v1.x

Onset-anchored windows; shape-aware readiness; contrast and delta proofs replacing subject matching; spread policy with a three-exit stopping rule; priced handoffs replacing generic next-step text; per-tenant telemetry configuration with service-homed endpoints; automated layout discovery writing default field mappings.
