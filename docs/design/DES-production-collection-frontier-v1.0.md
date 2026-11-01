# DES-production-collection-frontier v1.0

- Status: REVIEW
- Satisfies: `docs/requirements/REQ-investigation-loop-reengineering-v1.0` (G1, G3, S2, S7)
- Implements: `docs/architecture/ADR-PLANE-001-collect-distill-reason.md`; `DES-collect-frontier-v1.0`; `DES-reason-workorders-v1.0`
- Work package: WP-06

## 1. Purpose

This document defines the production integration boundary for the collection frontier. The owner has authorized implementation of the first lv14 vertical slice, but this document does not authorize migration completion, rollout, live battery execution, or a default-pin change. Full WP-06 acceptance remains open.

The accepted architecture separates cheap collection from expensive reasoning. The current repository already contains pure frontier and work-order contracts, but production dispatch still occurs through the ordered forcing cascade. This design defines how one authoritative collection chooser will eventually replace that split without reviving the deleted W1-A advisory layer.

## 2. Current state

- `domain/collection/frontier.py` provides pure candidate seeding, ordinal ordering, spread dimensions, depth/TTL admissibility, and exit evaluation, but it is not production-called.
- `domain/collection/work_orders.py` provides typed `REQUERY`, `GAP_FILL`, and `FETCH_SEED` carriers plus pure translation helpers.
- `application/worker/workflows/forcing.py` currently dispatches collection actions through ordered rules, including pivot, REQUERY, floor, re-read, call-site, log search, replan, and smallest-fetch.
- `RunInvestigationWorkflow` carries work-order entries beside turn history and dispatches the selected action through `execute_action_activity`.
- S2 dormant templates are compiled into model-facing context and name discriminating fetches, but they are not yet converted into an authoritative production frontier.
- W1-A deliberately removed the unused advisory emission/ranking outputs. The frontier library and work-order carriers were retained as design foundations and must not be reintroduced as an unconsumed advisory side channel.
- The first implementation slice makes the frontier authoritative only for locatable smallest-fetch work orders under `lv14`; unlocatable orders use the existing fixed kind-order fallback, and older pins retain the previous cascade behavior.

## 3. Goals and non-goals

### Goals

- Establish one authoritative chooser for admissible collection actions.
- Preserve tenant authorization, capability availability, lifecycle, defer, pause/cancel, budget, and terminal-verdict guards.
- Convert typed work orders, S2 discriminators, and eligible focus proposals into deterministic collection candidates.
- Preserve exact action parameters and work-order identity through selection and dispatch.
- Distinguish evidence progress, workflow progress, proof progress, clean empty results, deterministic misses, transient failures, and authorization blocks.
- Bound collection with explicit spread/tool/duration/token budgets and honest terminal outcomes.
- Support deterministic replay, restart reconstruction, candidate retirement, bounded retry, and evidence-driven novelty.
- Measure G1 movement against a fixed baseline and protect negative-probe behavior.

### Non-goals

- Do not add a queue, daemon, scheduler service, second provider executor, or second persistence/checkpoint system.
- Do not allow the frontier to prove a claim; ranking and collection outcomes are never verification authority.
- Do not revive W1-A `advisory_orders`, `advisory_ranking`, or the deleted advisory functions.
- Do not delete the retained pure frontier/work-order library in this work package.
- Do not change the default loop version or activate production frontier behavior without a separately approved implementation and rollout decision.
- Do not infer telemetry, expected information gain, or posterior confidence from missing data.

## 4. Authority boundary

The future production flow is:

```text
reason / verify / persisted evidence state
  -> typed candidate producers
  -> hard eligibility and authorization filters
  -> deterministic frontier ordering
  -> one selected action
  -> existing execute_action_activity
  -> evidence outcome and candidate state update
  -> verify / reason feedback edge
```

The frontier is authoritative only among collection candidates. The following remain outside frontier ranking and cannot be overridden by a candidate:

- pause, cancel, input suspension, lifecycle transitions, and terminal status;
- capability and tenant authorization;
- provider availability and typed denial;
- D6 opinion-only and absence-disconfirmation guards;
- duration, token, tool-call, evidence, and spread-budget enforcement;
- S6 verification and S7 conclusion/handoff decisions.

The implementation must expose exactly one selected collection action to the workflow. A direct forcing rule and a frontier-selected action must never both dispatch on the same turn.

## 5. Versioning

Production frontier semantics change loop behavior and therefore require a new recorded loop version after the current `lv13` line. The implementation must assign the next available version through the loop-version registry and route behavior with `loop_version_at_least`.

Rows pinned to earlier versions retain the existing forcing semantics. Unmarked rows resolve to the current version according to the repository loop-version contract. No environment variable may select frontier semantics.

Candidate and work-order payload versions remain separate from loop-version routing. Unknown candidate/work-order versions fail closed and are retired as invalid rather than guessed.

## 6. Candidate contract

The production candidate must carry:

- deterministic `candidate_id` derived from investigation, turn, source order, and canonical candidate identity;
- candidate/schema version;
- complete validated `action_type` and `action_parameters`;
- failure character and spread dimension;
- source identity: work-order ID, gap ID/reason, focus identity, or template identity;
- exact cost estimate and budget class;
- depth and TTL/expiry information;
- status and retirement reason;
- provenance sufficient to explain why it was eligible and selected.

No workflow-reachable candidate constructor may default to `uuid4()` or wall-clock time. Candidate creation must receive explicit deterministic values or omit nonessential timestamps.

Action parameters are validated against the existing action request/handler contracts before ranking. Invalid, unlocatable, unauthorized, stale, expired, or already-covered candidates do not enter the ranked set.

## 7. Candidate sources

### Typed work orders

`REQUERY` and `GAP_FILL` carriers remain the primary feedback edge from verification to collection. The frontier must preserve their existing identity and outcome-aware coverage semantics:

- a newer carrier supersedes an older carrier for the same slot;
- stale carriers expire by turn age;
- successful or deterministic-empty fetches cover the corresponding identity;
- transient failure/degraded outcomes may retry within a bounded attempt count;
- authorization blocks are terminal for that candidate;
- failed-cited-group work orders retain a distinct reason identity from uncited gaps.

### S2 dormant-template seeds

S2 templates provide discriminating fetches, cost estimates, assumptions, and cross-boundary handoff outcomes. In-tenant template fetches may become candidates only when their revision, target, action, and boundary are valid. Cross-boundary templates remain named handoffs and never become candidates.

### Focus proposals

The current bounded pivot logic is a candidate producer, not an independent dispatcher. Its existing cap, canonical surface comparison, pinned parameters, and owner-approved precedence must be preserved during migration. A pivot candidate must retain the model's requested parameters and revision rather than rebuilding a whole-file `HEAD` request.

### Existing forcing rules

Floor, re-read, call-site, log-search, replan, and walk-stack behavior must be classified during implementation as either:

- hard guard/candidate admission rule;
- typed candidate producer;
- retired behavior with a parity proof.

No rule may continue dispatching independently after frontier authority is enabled.

## 8. Evidence-state and spread mapping

The frontier consumes the typed `EvidenceState` contract:

- `NO_ROWS` -> layer spread;
- `ROWS_WITHOUT_MATCH` -> assumption spread;
- `STALE_MATCH` -> time spread;
- `ANALYSIS_INCOMPLETE` -> assumption spread;
- `ANALYSIS_BLOCKED` -> layer/provider spread;
- `LOCAL_COMPLETE` -> no new collection candidate from that character.

State derivation must use analyzer status, limitation codes, anchors, claim-binding outcomes, and banked evidence. A row count alone cannot distinguish clean empty, analyzer refusal, provider failure, or stale evidence.

The `RIVAL_SPREAD` dimension remains highest precedence when an unbeaten rival exists. Cross-boundary outcomes are resolved at seed time to named handoffs.

## 9. Ordering and budget

The initial implementation must choose one of two explicitly approved modes before code work. The accepted frontier contract defines score as expected-information-gain per cost multiplied by novelty (`docs/design/DES-collect-frontier-v1.0.md:7-9`), while its rebuild notes explicitly describe the current score as ordinal and uncalibrated (`docs/design/DES-collect-frontier-v1.0.md:23-25`; `docs/design/DES-reason-workorders-v1.0.md:23-25`):

1. **Deterministic provisional order:** fixed kind precedence and deterministic candidate identity ordering until outcome data calibrates information gain.
2. **Measured best-first order:** expected-information-gain-per-cost multiplied by novelty, with a bounded fallback for unlocatable candidates.

The current design recommends mode 1 for the first production wiring tranche. The existing frontier score is ordinal and its exit thresholds are uncalibrated; this is documented in the accepted frontier rebuild notes (`docs/design/DES-collect-frontier-v1.0.md:23-25` and `docs/design/DES-reason-workorders-v1.0.md:23-25`). Making it authoritative before calibration would convert an advisory heuristic into an unverified behavior change.

Regardless of mode:

- cost is monotone in fetch size/search breadth and clamped to a finite positive range;
- no malformed/non-finite cost may empty the candidate set or raise from ranking;
- spread budget, tool/evidence limits, duration budget, and token budget are separate constraints and are checked before dispatch;
- budget exhaustion produces the existing priced S7 handoff path, not a bare abstention;
- fixpoint, diminishing returns, and confidence-with-corroboration require explicit measurable inputs and must not be inferred from absent data.

## 10. Workflow and activity integration

The workflow owns only deterministic candidate state, selection, and carrier stamps. Activities own evidence reads, provider calls, authorization, persistence, and analyzer summaries.

Each selected candidate is dispatched through `execute_action_activity`. The workflow records:

- selected candidate/source identity;
- action and bounded parameters;
- cost and budget result;
- outcome status and miss reason;
- evidence IDs/count and analyzer status;
- candidate retirement, retry, or coverage decision.

On worker restart, the frontier is reconstructed from checkpoint state, action history, typed carriers, and persisted evidence summaries. No process-local frontier state is authoritative.

## 11. Replay and determinism requirements

- No `datetime.now()`, `uuid4()`, environment read, provider call, database read, or mutable process-global state in workflow candidate creation, ranking, exit evaluation, or coverage decisions.
- Canonical JSON and deterministic sorting define candidate identity and tie-breaking.
- Candidate parameters must be strict JSON-safe values.
- Replay tests must cover the same candidate set with different input ordering, stale/new carriers, failure/retry outcomes, restart reconstruction, and budget boundaries.

## 12. Acceptance plan

### Unit and pure-contract gates

- Candidate identity, strict payload validation, cost clamping, deterministic ordering, novelty baseline, spread mapping, depth/TTL, and exit precedence.
- Work-order translation for locatable and unlocatable orders, cross-boundary refusal, unknown versions, duplicate identities, stale carriers, and failed/degraded retry outcomes.
- Negative controls for bypassing authorization, treating ranking as proof, and allowing malformed candidates to dispatch.

### Workflow/replay gates

- One-candidate dispatch per turn.
- No direct forcing dispatch after frontier authority is enabled.
- Requery/gap/pivot precedence and owner-approved pivot behavior.
- Restart reconstruction and Temporal replay with recorded action outcomes.
- Budget exhaustion, fixpoint, stale-carrier, retry-bound, provider-block, and cancellation behavior.

### Battery and G1 gates

- Record the current-code-shape 105-case baseline before implementation.
- Re-grade the targeted collection stages with the same inputs and pinned configuration.
- Measure bound-or-priced-handoff rate using only concrete proved conclusions, localizing evidence, or concrete priced next fetches.
- Require no regression on negative/absence probes.
- Report collection cost, evidence progress, proof progress, retry counts, stale/invalid candidate counts, and terminal reasons.
- Do not claim G1 closure from unit tests or a single live run.

## 13. Open decisions before implementation

- **Authority scope:** replace all collection-producing forcing overrides in one tranche, or first make the frontier authoritative only for the smallest-fetch slot? The P6 implementation plan recommends the former (`docs/IAP-S1-S7-reengineering-implementation-plan-v1.0.md:76-83`); the safer staged implementation would need an explicit versioned intermediate contract.
- **Initial ordering:** deterministic provisional order or measured best-first score? This design recommends deterministic provisional order until information-gain/cost calibration exists.
- **Loop version:** assign the next loop version after `lv13` at implementation time; do not reserve a literal placeholder in code.
- **Confidence exit:** identify the recorded verifier/posterior signal and independent artifact-type count before enabling the corroborated-confidence exit.
- **G1 target:** confirm whether WP-06 acceptance requires measurable battery movement or only production wiring plus an honest baseline.

## 14. Non-claims

This design does not claim that the frontier is production-wired, that the accepted G1 target is met, that Temporal replay passes, or that the 105-case battery has been rerun. Those are implementation and acceptance evidence requirements.
