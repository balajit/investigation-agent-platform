# ADR-KERNEL-003: Atomic Publication with Hypothesis CAS on Context

- **Status:** Accepted
- **Date:** 2026-10-23
- **Deciders:** Sole engineer (owner); assistant as chief architect (draft)
- **Scope:** Publication path (`VerificationEngine` + `ConclusionGate` + `application/worker/activities.py` publish path + persistence repositories)

## Context

A version check followed by an unguarded write cannot guarantee that the evaluation context is still current at commit time. As built, investigations have optimistic CAS (`infrastructure/persistence/investigation_repository.py:149-183`) but hypotheses and conclusions are last-write-wins (`hypothesis_repository.py:97-130`, `finding_repository.py:214-249`), so a stale `SATISFIED` could survive a lost premise. Package: `docs/IAP-proof-kernel-contracts-v1.0.md` §5.1, design §2.5.

## Decision

- Publication commits through one linearization point: a conditional update on `(investigation_id, context_id, version)` (database transaction, CAS, or equivalent). Unguarded status writes are prohibited.
- Protocol: establish context → validate certificate + premises → verify reconciliation + policy inputs → confirm context still current at commit → commit referencing the exact certificate + context ID, never a bare hypothesis-level `VERIFIED`.
- Hypothesis persistence gains CAS + context check (Stage C entry criterion); conclusions carry `context_id` with the same check.
- On context change: reevaluate or deny; never mutate the old certificate. Dependency classes (proof / publication / operational) govern what invalidates what.

## Consequences

- Positive: K8/K15 races close; mixed-snapshot publication becomes impossible; every published conclusion is traceable to one certificate + context.
- Negative: write-path changes across three repositories plus migration; contention and retry semantics must be tested with controlled interleavings.
- Neutral: technology choice (transaction vs CAS) is free; atomicity is mandatory. Legacy writers are inventoried and routed through the transition API before the kernel becomes authoritative.
