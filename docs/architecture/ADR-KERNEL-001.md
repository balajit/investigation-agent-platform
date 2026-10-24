# ADR-KERNEL-001: Closed Inference-Rule Registry with Implementation-Digest Binding

- **Status:** Accepted
- **Date:** 2026-10-23
- **Deciders:** Sole engineer (owner); assistant as chief architect (draft)
- **Scope:** Proof kernel rule authority (`application/investigation/verification.py`, future `application/investigation/` registry module)

## Context

The kernel design requires that a certificate checker prove a derivation follows the implemented rules, and that rule identity binds the executed implementation rather than a version string alone. The current `check_claim_kind` ladder plus `CATEGORY_PROOF_RULES` has no registry versioning or artifact digest, so a rule implementation could change without its declared identity changing. Package: `docs/IAP-proof-kernel-contracts-v1.0.md` §3.2.

## Decision

The kernel adopts a finite, versioned, closed inference-rule registry:

- One row per `ClaimKind` (`DEFINITION`, `CALL_EDGE`, `BEHAVIOR`, `ABSENCE`, `HISTORY`, `CAUSAL`), each with exact input/output types, mechanically checked preconditions, explicit non-mechanical assumptions, a soundness label (`SOUND` / `CONSERVATIVE` / `HEURISTIC`), documented unsupported cases, and an implementation digest (SHA-256 of the build artifact).
- No arbitrary logic expressions, proof search, or configurable inference language until a demonstrated need the typed rules cannot meet.
- `HEURISTIC` rows are never proof-producing in a publishable certificate.
- Initial kernel ships `DEFINITION` + `CALL_EDGE` only; `BEHAVIOR` next; `ABSENCE`/`HISTORY`/`CAUSAL` only with contracts + independent goldens.
- Release control fails the build when an implementation changes without its declared identity changing (K9).

## Consequences

- Positive: rule drift is detectable; the checker has a stable contract; K1/K9 goldens are writable today.
- Negative: every rule change pays registry + digest + fixture cost; no ad-hoc inference.
- Neutral: current `verification.py` kind functions become the first registry implementations behind the interface; no behavior change until the Stage B gate passes.
