# REQ — Investigation-loop re-engineering v1.0

- Status: ACCEPTED
- Satisfied by: docs/design/DES-* (see traceability in each design); motivated ADRs in docs/architecture.

## 1. Problem

When a production issue occurs, the investigation loop must determine the cause or hand off a priced next step. The loop currently abstains on whole question classes (definition, caller enumeration, history, path) and grades its own readiness with shape-unaware checks. Past effort patched individual gates; the missing pieces are structural: no onset anchor, no windowed evidence, no contrast-shaped proofs for dormant bugs, no separation between cheap gathering and expensive reasoning, and no feedback edge from verdict back to collection.

## 2. Goals (measurable)

- G1: Bound-or-priced-handoff rate on the 105-case battery: every terminal state is a proved conclusion, a redirection with localizing evidence, or a priced next fetch. Bare abstention is not a terminal state.
- G2: Recall-at-brief vs compression: the distill plane retains the evidence needed for the bound verdicts at a target of 5–10% of raw tokens, with a faithfulness floor as a hard gate.
- G3: Shape-aware stage honesty: S4 readiness passes iff qualifying evidence is banked for the question's shape; S6 verifies or fails on actual binding results, never auto-passes; S7 states the discriminating next check or the elimination record.
- G4: Onboarding contract: per tenant and per service, a measured statement of which branches are active and which are blocked on missing telemetry.

## 3. Non-goals

- No fetching across the tenant boundary: services outside the tenant's declared repos are never queried; they are named handoffs with localizing evidence.
- No forensics beyond identify-the-area: the loop localizes the problematic area; deep forensic reconstruction is out of scope.
- No full causal discovery per incident: constraint-based graph learning is ruled out for short windows (sample-starved, faithfulness violations); structure comes from the architecture, testing from the window.
- No certification workflow for discovered mappings in v1 (deferred).

## 4. Constraints

- C1: Tenant-repo boundary is a hard stop for all widening.
- C2: No legacy branches; behavior changes are version-pinned, never env-switched.
- C3: Secrets (endpoint credentials) live in tenant operator scope, never in the repo, never in logs.
- C4: Required runtime evidence is load-bearing: its absence blocks and logs at ERROR, never degrades silently.
- C5: Discovered mappings are defaults the operator may edit; auto-discovery never overwrites hand-edited mappings.

## 5. Acceptance criteria per stage

- S1: every investigation records onset T and last-known-good G, or explicit unknown; spread budget assigned from the tool-call envelope.
- S2: hypothesis classes include the four dormant-activation templates, each naming its discriminating fetch.
- S3: authorization/availability gaps over widened evidence appear as visible gaps, never silent empties.
- S4: range rows qualify on all four pinned fields; history rows on type + path + revision + scope relevance; absence only on structured complete-empty proofs; runtime rows on window pinning; contrast pairs qualify jointly.
- S5: causal conclusions name the strongest rival ruled out and the evidence that ruled it out.
- S6: answered history claims pass only on verifier confirmation; proven-absence INSUFFICIENT passes; unbound INSUFFICIENT routes into spread, not into stop.
- S7: every terminal state carries the single highest-value next fetch given the failure character, or the elimination record.

## 6. Glossary

Onset T, last-known-good G, evidence brief, work order, frontier, handoff, elimination record, spread budget, recall-at-brief, faithfulness floor, contrast proof, assumption-contrast, discriminating fetch, BLOCKED_ON_TELEMETRY, DEFERRED, WAITING.
