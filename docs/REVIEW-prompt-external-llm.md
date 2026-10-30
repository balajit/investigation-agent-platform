# Codebase review request — Investigation Agent Platform (IAP), loop-reengineering branch

## What this system is

IAP is a Temporal-orchestrated, Postgres-backed (pgvector) investigation agent for
code incidents. A user files a complaint ("Where is SampleOrderFilter implemented?");
a loop of **reason → execute → retrieve → verify** turns runs until verification,
a forced terminal override, or an iteration bound (max 10), then concludes with a
cited verdict or an honest abstention. Multi-tenant: every request carries
`X-Tenant-ID`; an `application_profiles` row per tenant gates which evidence types
and capabilities (SEARCH_CODE, GET_CODE, SEARCH_LOGS, …) the agent may use — the
authorizer fail-closes (no profile ⇒ deny; `allowed_capabilities` non-empty and
missing the capability ⇒ deny).

**Repo layout** (`src/investigation_agent_platform/`):
- `application/worker/workflows/run_investigation.py` — the Temporal workflow
  (`_run_inner` holds the main `while iteration < max_iterations` loop).
- `application/worker/workflows/forcing.py` — the post-reason forcing cascade
  (pure functions; ~2600 lines): pivot recollect, walk-stack, G3 requery,
  smallest-fetch, evidence floor, formulate/synthesis/callsite/log-search/replan.
- `application/investigation/reasoning.py` — `ReasoningCoordinator` (LLM reason
  step, schema validation with bounded hidden resamples).
- `application/investigation/verification.py` + `worker/activities/verification.py`
  — claim binding (phases 1–4), `select_ledger_requery`, `build_claim_gaps`.
- `application/evidence/` — retrieval gateway (authz enforcement), retrieval
  ledger (attempt counting, `directive_consumed`), history sanitization for prompts.
- `application/analysis/java/` — Tree-sitter-based Java facts (`tree_sitter_language_pack`;
  `method_facts`, `field_facts`, `dataflow`, `flow_facts`, `boundary_facts`,
  `window_facts`, …) plus `python/` equivalents and Spring-config analysis.
- `domain/collection/` — typed collection layer: `frontier.py` (best-first
  candidates, novelty, spread dimensions), `work_orders.py` (PENDING orders with
  failure character, cost, deterministic IDs), `focus.py` (file/symbol focus,
  shift detection).
- `infrastructure/` — Postgres persistence (SQLAlchemy), pgvector retrieval,
  `security/profile_authorizer.py`, telemetry.
- `scripts/tranche_b_battery.py` + `tests/fixtures/battery_cases/*.json` (L1–L13,
  6 live) — the graded live battery; G1 = bound-or-priced-handoff rate.
- `scripts/diagnosis.sh` — single-case runner printing API verdict + DB stage
  info + condensed worker-log turns.

**Branch context:** `fix/loop-l1-reread` (merged into `iap-s1s7-reengineering-v1.0`,
~20 commits) re-engineered the collection layer end-to-end ("2b"): (1) range reads
bank decisive evidence again (a `discovery_only` stamp bug had blinded them) and
the readiness-0.95 loop exit was deleted — the loop now runs to its bound;
(2–4) the cascade was extracted verbatim into `apply_post_reason_cascade` + a
typed advisory emission/ranking layer that records but never dispatches;
(5) the G3 requery slot dispatches carried REQUERY work orders (marker dicts
retired); (6) the smallest-fetch slot dispatches carried GAP_FILL orders
(feed dicts, `_smallest_fetch_target` retired); (7) phase gate: unit 4485 green,
DB 19/19, ruff clean, live battery 5/6 PASS (P0 baseline was 1/9).

## Review areas and the exact questions

### 1. Hypothesis → verification pipeline (highest value)
Trace one hypothesis from birth to verdict: `ReasoningCoordinator` formulates →
`build_claim_gaps` / claim binding partitions (phases 1–3: partitions, demotion,
stored queries, cbv3 repair sub-gate) → verify (`select_ledger_requery`,
`verify_root_cause_activity`) → conclude. Choices we made — challenge each:
- Claim identity is `(kind, target_key)`, statement text excluded, so reworded
  claims re-resolve to the same `gap_id` (rephrase-stability). Is keying too
  coarse — can two genuinely different claims collide on one target?
- Uncited claim groups produce fetch gaps; cited-but-failed groups go to
  repair/obligations, never to smallest-fetch. Is that partition sound, or can
  a failing citation need a re-fetch rather than a repair?
- Verification is claim-local (per-gap/per-claim binding + elimination records
  in S7 handoff). Does anything verify the *conjunction* of claims behind a
  verdict, or can individually-verified claims compose into an unsound conclusion?
- `NOT_CHECKABLE` claims are excluded from the publish denominator (honest
  abstention path). Is the checkability classifier itself trustworthy — what
  stops the loop from labeling hard claims NOT_CHECKABLE to abstain cheaply?
- Bounded hidden LLM resamples on schema/cardinality failure (up to 2), then
  `reasoning unavailable` fails the whole run (not INSUFFICIENT). We know this
  crash path fires on sampling noise (L3 flaked 3×). Should transient invalid
  output degrade instead of fail, and where is the line between transient and
  provider-dead?

### 2. Forcing cascade precedence (`forcing.py`)
Eleven overrides fire in a fixed order with guard flags (`forced_walk_stack`,
`forced_requery`, `forced_pivot_recollect`, `forced_smallest_fetch`, …).
Later rules stand down on earlier flags; walk-stack suppresses eval entirely;
D6 opinion-only fast path outranks everything. Review for:
- Precedence inversions: is there any turn where two rules legitimately fire
  and the loser carried the better fetch? Pay special attention to
  ledger-requery vs smallest-fetch vs pivot vs floor ordering.
- Guard completeness: every rule that can fire on a pivot turn must check
  `forced_pivot_recollect` (pinned by `TestPivotPrecedence` — is the pin list
  exhaustive, and does the AST contract test actually cover the helper
  structure?).
- The `forced_*` flags are strings in a dict — a typo'd flag silently disables
  a guard. Should these be an enum/typed model?
- `apply_post_reason_cascade` mutates `_history` entries in place (consumed
  stamps) inside a Temporal workflow: replay-safety argument is "history is
  workflow state, deterministic functions only." Stress-test that claim —
  any wall-clock, randomness, or iteration-order dependence in the cascade?

### 3. Work orders, frontier ranking, carriers (the new 2b layer)
- Orders are PENDING records with deterministic uuid5 IDs; dispatch slots
  translate them to frontier seeds per turn. Is there any path where an order
  dispatches twice (replay, retry, duplicate history entries), and does
  idempotency hold if the workflow re-executes a turn?
- Newest-carrier-only reading + orders-empty tombstone supersession: can a
  tombstone suppress a still-open order that a *later* verify re-issues —
  and is that the desired semantic?
- Ranking is advisory-only and never consumed for dispatch (rank 1 is
  informational). Is the ranking pulling its weight, or dead complexity?
  If kept, should G3/smallest-fetch dispatch from rank order instead of
  newest-first?
- Pivot still dispatches from `FocusPoint` directly (advisory FETCH_SEED never
  dispatches). Is the split — typed focus + advisory seeds — coherent, or
  should pivot consume orders like the other slots (candidate step 8)?

### 4. Java parsing (`application/analysis/java/`)
Tree-sitter via `tree_sitter_language_pack`, per-concern fact modules,
windowed reads (definition windows ~13–32 lines pinned by range-evidence
banking; path-only GETs stay `discovery_only` and do NOT count as decisive).
Review for:
- Grammar/version skew: what happens on newer Java constructs (records,
  sealed types, pattern matching) the queries don't cover — silent gaps or
  loud failures? Silent gaps would starve evidence without a signal.
- Window correctness: can a definition window split a method signature from
  its body, or attribute a nested/lambda block to the wrong method? Check
  `window_facts.py` boundary logic and the range-read banking guard in
  worker `execution.py` (`_is_range_read` vs `discovery_only`).
- `boundary_facts.py` / cross-boundary termination: are module/service
  boundaries detected or assumed from repo layout? A wrong boundary means a
  named handoff that should have been a fetch, or vice versa.
- Dataflow (`dataflow.py`): intra- vs inter-procedural reach — where does it
  stop, and is the stopping point documented at the call site or silently
  conservative? Conservative dataflow + the loop's "no rows" spread rule
  interact: does the frontier correctly escalate when dataflow bottoms out?
- Spring-config analysis: does it handle property-source precedence
  (YAML > properties > env > defaults), or can it cite a value that runtime
  would override?

### 5. Retrieval, authorization, tenant isolation
- Two deny reasons exist (`Agent unauthorized…` vs `Capability … is disabled`)
  from profile gates (`enabledEvidenceTypes`, `allowedCapabilities`). The
  n-batt battery profile denies SEARCH_LOGS every turn — the loop survives on
  code-intelligence alone. Is fail-closed + degrade-to-empty the right
  behavior, or should a fully-denied capability class fail the run loudly?
- `recorded_attempt_summaries` + `directive_consumed` do attempt-budget
  accounting across history stamps and same-turn summaries. Find the
  double-counting or under-counting edge (the code comments admit one was
  already fixed via `attempt_id` stamping — look for the next one).
- Tenant isolation: history, evidence, stamps, and carriers are all keyed by
  tenant — verify there is no cross-tenant read path (retrieval, ranking,
  telemetry joins), especially in shared caches or the knowledge/retrieval
  layers.

### 6. Termination, honesty, metrics
- The loop terminates on verification, forced overrides, D6, or the iteration
  bound. The removed readiness exit means an overconfident-but-unverifiable
  model burns all 10 turns. Is the bound priced (each turn costs LLM +
  provider calls) — and should overconfidence accelerate termination instead
  of just incrementing a critic counter?
- Honest-abstention paths (D6, absence, INSUFFICIENT): can the loop reach a
  verdict with zero banked rows, and is every such path labeled as abstention
  rather than an answer? Check `conclude` for fail-open verdicts.
- Battery/scoring (`tranche_b_battery.py`, `battery_cases/`): are the graders
  checking cited evidence or verdict strings? A verdict-string grader can be
  gamed by a fluent model with no bindings — confirm the PASS bar requires
  bindings.

## Open items we already know about (confirm, extend, or refute)
1. Reasoning-unavailable → run FAILED propagation (should degrade?).
2. L5 stays INSUFFICIENT at every baseline (question-answering breadth — is
   the loop structurally incapable of semantic synthesis, or just unguided?).
3. 286 files fail `ruff format --check` and mypy reports 512 errors repo-wide
   (both red at baseline; our files are check-clean). Is the static gate
   salvageable or should it be re-scoped?
4. `diagnosis.sh` shows log lines go silent after `start-platform.sh --restart`
   truncates `tmp/*.log` — evidence loss on every restart.

## How to deliver the review
- File-by-file findings with `path:line` references, each rated
  **blocker / should-fix / consider**, with the concrete failure scenario
  (not style nits — we have ruff for that).
- For every hypothesis→verification choice in §1, state **agree / disagree +
  counterexample**.
- End with the top 3 risks you would fix before this system touches a
  real tenant, and what single experiment would best validate each.
- Assume the test suite (4485 unit, 19 DB, live battery) as supporting
  evidence, not proof — point at what the tests *don't* pin.
