# ADR-KERNEL-005: Shadow-Planner Boundary Inside the Existing Workflow

- **Status:** Accepted
- **Date:** 2026-10-23
- **Deciders:** Sole engineer (owner); assistant as chief architect (draft)
- **Scope:** Investigation action selection (`application/worker/workflows.py` + `activities.py` loop, future pure policy in `application/investigation/`)

## Context

The workflow accumulates forcing heuristics (`_should_force_*`, capped at 2/run) with no typed obligation input, while the verifier emits increasingly structured diagnostics (`next_check`, limitation codes) with no routable obligation output. The planner must acquire missing evidence without ever manufacturing certainty, and the existing durable workflow stays the sole executor. Package: design §2, plan WP-D2/D3, sixth-review Steps 5–6.

## Decision

- A pure `InvestigationActionPolicy` maps open obligations + history + capabilities + budgets to one valid action + rationale + expected discriminator + bounded cost. Three responsibilities only: name the unsatisfied premise, enumerate capable actions, select via explicit priority rules with retry caps and budgets.
- The model ranks already-admissible actions at most; the deterministic policy validates the choice before execution. Expected-information-gain ordering applies only where multiple genuinely viable actions exist and measurements support it.
- Shadow-only until G6: no side effects, no execution, no authoritative-state mutation; deterministic replay over captured snapshots; divergence measured on choice, predicted discriminator, evidence novelty, proof progress, and cost with bounded reason codes.
- Exactly one authoritative executor at all times (legacy forcing until per-family parity). Migration proceeds family by family (definition → call-site → runtime → absence/history) with rollback; a forcing helper is removed only after parity on its mapped cases. Workflow, evidence, and proof progress are tracked separately; exhaustion ends in typed abstention.

## Consequences

- Positive: testable routing, attributable next actions, honest abstention; no second runtime or checkpoint store.
- Negative: stable obligation/action vocabulary + parity harness cost per family; deterministic table can false-reject viable paths (capability-aware fallback required).
- Neutral: proof rules unchanged in the first slice; planner decisions never enter a derivation.
