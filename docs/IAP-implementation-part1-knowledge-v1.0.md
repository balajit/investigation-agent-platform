# IAP Implementation Part 1 v1.0: Core Platform — Domain, Ports, State Engine

Supersedes `docs/archive/IAP-implementation-part1-py-v1.md` (archived for its stale §4–§5 contracts; the "As built" notes folded in here as fact). Verified against code 2026-09-27. Living issues: `docs/IAP-issues-0927-wip.md`.

## Facts (current platform)

Hexagonal layout under `src/investigation_agent_platform/`: `domain/` (zero infra deps except Pydantic v2), `application/`, `ports/` (`typing.Protocol`, runtime-checkable), `infrastructure/`, `api/`, `bootstrap/`.

Domain aggregates (`domain/`): `Investigation` (aggregate root, UUID, optimistic `version`, `code_issue_fingerprint`), `InvestigationRequest/Context/Status/Transition/State`, `Fact`/`FactType`, `Evidence` (+`EvidenceReference`, `EvidenceRelationship` with 12 relationship types, fingerprint-required, redaction manifest, tier-2 content invariant), `Hypothesis` (6 statuses + evidence assessments), `Finding`/`InvestigationConclusion` (4 conclusion statuses), `ApplicationProfile` (observability/state/code/correlation/investigation sub-profiles; `CodeProfile.repository_id` + `service_name` link Layer 3).

Ports (`ports/`): `InvestigationTriggerPort.trigger(tenant_id, request)->UUID` (single op; lifecycle ops live on services); provider protocols take leading `tenant_id`/`investigation_id` with typed requests (`RuntimeEvidenceRequest`, `ApplicationStateRequest`) and the `EvidenceQueryResult` envelope; `CodeEvidenceProviderProtocol` (raw source/history/diff) split from `CodeIntelligenceProviderProtocol` (symbols/callers/callees/handlers/DB-ops returning `CodeSymbol`/`CallGraphNode`/`CodeLocation`); `CorrelationEngine.correlate(...)→CorrelationResult` plus `CorrelationExpander`; `InvestigationReasoner.reason(tenant_id, state)→InvestigationDecision`.

Services (`application/investigation/services.py`): Create/Get/Resume/Cancel + `InvestigationExecutionContext` (no `open_questions`/`pending_actions` — those live on checkpoints and action records). Resume restores investigation + profile + evidence + timeline; re-derivation happens in workflow activities. Cancel persists the transition; halt is Temporal-signal-based.

State engine: Temporal `RunInvestigationWorkflow` is the single orchestrator (create → contextualizing → investigating → correlate/hypothesize/verify loop → concluding → completed/failed/cancelled, plus `PAUSED` holding state and `COMPLETED/FAILED → INVESTIGATING` reopen edges). All DB writes occur inside idempotent activities. Safety: `InvestigationActionValidator` + 8 platform rules (no dynamic SQL — static `StateProfile` templates validated by `sqlglot`; no direct Elastic/shell/file access; profiles carry no secrets).

Persistence: SQLAlchemy 2.0 async + Alembic (7 revisions), tables `timeline_events`, `investigation_transitions/facts/entities`, `evidence_references`, `findings`, `investigation_conclusions`, `investigation_actions`, `investigation_tool_executions`, `application_profiles` (single, `profile_json`), plus `idempotency_keys`, `outbox_events`, `action_executions`, knowledge/topology tables. RLS on `tenant_id`; OCC via `version`.

## Decisions kept

Reasoning/evidence separation (ADR-001..004); evidence as first-class domain object; deterministic correlation over LLM inference; MCP as adapter, never architecture; tenant-first signatures on every port; reopenable terminal states for recurring issues.

## Future extensions

- **Manageability**: generate the port matrix docs from `runtime_checkable` protocols (single source; the drift that motivated this rewrite cannot recur); add `GET /profiles` create/update behind admin auth.
- **Scalability**: `InvestigationLimits` are per-investigation; add per-tenant concurrency quotas at the trigger port before multi-tenant load.
- **Performance**: evidence bulk paths (`save_batch`) exist — extend batching to timeline appends on high-event investigations.
