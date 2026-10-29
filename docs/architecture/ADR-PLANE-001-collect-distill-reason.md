# ADR-PLANE-001: Collect / distill / reason separation

- Status: ACCEPTED
- Motivates: REQ-investigation-loop-reengineering-v1.0 (G1, G2)

## Context

The reasoner (LLM, charged per token) currently drives every fetch, so gathering is bounded by expensive inference and stops when the reasoner gives up. Human practice inverts this: cheap homework first (days/weeks of gathering), the expensive expert sees only a distilled brief plus targeted questions back.

## Decision

Three planes with distinct cost profiles and optimization targets:

1. Collect (cheap, deterministic, runs to fixpoint): git walks, log/trace scans, diff computation, call-graph traversal, config snapshots. Optimizes recall per unit collection cost via a best-first frontier scored by myopic expected-information-gain per cost with a diversity penalty.
2. Distill (cheap models, streaming): rerank, sentence-prune, extract into a query-focused brief with citations and a dissent section. Optimizes compression at fixed recall with a faithfulness floor as a hard gate.
3. Reason (expensive, rare): reads only the brief plus content pointers; outputs verdicts and work orders (discriminating next fetches) back to collection.

Escalation to the strongest reasoner is threshold-gated and calibrated on labeled replays, with corroboration requirements and audit escalation against confidently-wrong cheap tiers.

## Consequences

Positive: gathering runs long without token cost; the reasoner stays lean; failure of a fetch widens the search instead of stopping it. Negative: two new components (frontier scorer, distiller) to build, calibrate, and monitor; brief quality becomes the load-bearing property of the system.

## Alternatives considered

Status quo (reasoner-driven fetching): rejected — couples recall to token spend and has no feedback edge. Full causal discovery per incident: rejected — sample-starved on short windows, faithfulness violations routine in ops data.
