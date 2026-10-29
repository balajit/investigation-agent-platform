# DES-S2-dormant-templates v1.0

- Status: ACCEPTED
- Satisfies: REQ-investigation-loop-reengineering-v1.0 (S2, G1)
- Implements: ADR-PROOF-001 (contrast framing); IAP-investigation-architecture-v2.0 (stage map)

## 1. Templates

For each suspect path, S2 instantiates four dormant-activation templates alongside mechanism hypotheses. Each template names its discriminating fetch at birth:

- Flag-gated: flag-gates on the path × flag-state records. Discriminator: gate state at T vs G.
- Data-gated: enum/switch/match sites × recent payload variety from traces. Discriminator: first-seen payload shape per branch.
- Contract drift: external-call sites × current upstream response vs assumed shape. Discriminator: live response shape against the code's assumption.
- Regime crossing: loops/pools/retries × metric trends. Discriminator: trend break aligned with T.

## 2. Assumption enumeration

Every branch/guard on the suspect path renders as an explicit checkable assumption (expects X). Assumptions are checked against present reality in cost order; each check is priced against the spread budget.

## 3. Cross-boundary rule

Templates that resolve beyond tenant repos terminate at template time as named-handoff candidates with the localizing evidence attached (see ADR-BOUNDARY-001), never as fetches.
