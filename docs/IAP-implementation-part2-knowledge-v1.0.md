# IAP Implementation Part 2 v1.0: Reasoning Agent and Orchestration Loops

Supersedes `docs/archive/IAP-implementation-part2-py-vi.md`. Verified against code 2026-09-27. Living issues: `docs/IAP-issues-0927-wip.md`.

## Facts (current platform)

Reasoning stack (`application/investigation/`): `ReasoningCoordinator` (implements `InvestigationReasoner`; provider-agnostic `LLMGateway` via `infrastructure/reasoning/factory.py` with OpenAI + Anthropic adapters and `pricing.py`), `ReasoningContext` + `EvidenceContextFormatter`, `InvestigationPlanner` (`InvestigationPlan` of typed `InvestigationStep`s with `StepStatus`), `InvestigationLoopGuard` (bounded iterations; workflow backstop `WORKFLOW_LOCAL_ITERATION_GUARD=10`), `ActionValidator` protocol + `InvestigationActionValidator`, `InvestigationBudget` policy model alongside domain usage-tracker `domain/investigation/budget.py::InvestigationBudget` (`is_exceeded()`), `VerificationEngine` + `RootCauseVerificationPolicy` + non-LLM `InvestigationConfidence` formula, `ConclusionGate` (default 0.80 threshold) as the terminal authority, `InvestigationMemory`/`OpenInvestigationQuestion` for open threads.

Determinism controls: action fingerprinting (SHA-256 over canonical JSON) with dedup; budget ownership documented — operator knob is infra `BudgetConfig` (`IAP_MAX_TOOL_CALLS`=50, `IAP_MAX_REASONING_CALLS`=20, `IAP_MAX_DURATION_SECONDS`=1800), never code constants. Sanitization at ingress (Presidio + patterns) with XML isolation envelopes for untrusted payloads; two-tier payload offload (Postgres snippets, S3/MinIO bodies).

Not built (downgraded by decision): no `domain/contradiction` aggregate — contradiction lives in `HypothesisEvidenceAssessment`, `EvidenceRelationship(CONTRADICTS)`, `VerificationEngine.Contradiction/CausalRelationship`, and `contradicting_evidence_ids` (see `docs/archive/dev/architecture.md` B7 note); no deterministic no-LLM replay harness or agent-evaluation recorder.

## Decisions kept

LLM proposes typed intents; platform validates, executes, and persists. Confidence is computed, not asserted; `INCONCLUSIVE` beats a confident wrong answer; capture failures never fail investigations.

## Future extensions

- **Manageability**: no-LLM replay harness (fixture → `ROOT_CAUSE_ESTABLISHED`) for regression-testing reasoner changes without model spend; evaluation recorder for precision tracking.
- **Scalability**: per-tenant reasoning-call quotas; sliding-window context accounting per investigation.
- **Performance**: streaming reasoner responses for the UI path; cache stable prompt prefixes where the provider supports them.
