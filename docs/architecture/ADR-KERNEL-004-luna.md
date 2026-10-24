# ADR-KERNEL-004: Generic Witness Contracts, Shadow-Only Pilots

- **Status:** Proposed — owner decision recorded in the non-Luna ledger; this copy requires ratification within the Luna package before it governs implementation.
- **Date:** 2026-10-23
- **Deciders:** Sole engineer (owner); assistant as chief architect (draft)
- **Scope:** Optional analyzer path-witness and constraint-solver evidence pilots; native proof model untouched

## Context

External evidence producers (a revision-pinned analyzer producing path witnesses; a narrow solver checking admitted value/branch predicates) can add value only where the validated native core is demonstrably thin. Without a generic contract, each tool grows its own evidence system plus reconciliation adapters, duplicating the architecture the kernel exists to prevent. No analyzer or solver output is authoritative today, and pilots must keep it that way. Package: `docs/IAP-proof-kernel-ops-review-v1.0-luna.md` §6, `docs/IAP-proof-kernel-contracts-v1.0-luna.md` §§3–4, and `docs/IAP-proof-kernel-plan-v1.0-luna.md` WP-E1/E2.

## Decision

- Proposed for owner ratification: the native proof model defines tool-independent generic contracts first; adapters produce them, never define them:
  - Path-witness record: node/edge identities, ordered segments, source/sink roles, completeness, truncation flags, source revision, analysis configuration (query/model/version), limitations.
  - Solver result: encoded predicate, assumptions, omissions, solver status + termination reason, model-to-source mapping, independent-check state. Four result classes only: encoded-predicate-proved, abstract counterexample, source-grounded counterexample, unknown-or-inadmissible.
- Native rules consume the generic contract with no tool-specific logic. Truncated/incomplete/mismatched/timeout/unavailable witnesses are discovery-only or typed limitations — never positive proof, never absence proof.
- Both pilots run shadow-only, off the critical path, with timeouts, cancellation, and fallback. Promotion requires predeclared bars: usable-path and mismatch/truncation rates, latency, storage, model/encoder maintenance, encoder-fidelity goldens per admitted family (solver), zero native-verdict flips.
- Ordering: kernel, then edges/routing, then witnesses. Witnesses never shape the native proof model.

## Consequences

- Positive: at most two narrow, measurable bets with kill switches; native authority never diluted.
- Negative: adapter + goldens + scorecard cost even for declined pilots; per-family encoder review is the dominant cost on the solver side.
- Neutral: declining either or both pilots leaves the vocabulary intact with no dependency added.
