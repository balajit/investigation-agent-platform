# Functional Gap Analysis v0.1 — Investigation Agent Platform

- **Scope:** agentic decision-making, evidence utilization, reasoning loop, conclusion quality. Syntax and style are out of scope.
- **Reviewer:** Sonnet (principal AI systems / autonomous-agent audit lens).
- **Source prompt:** review prompt recorded at review time (working notes, unshipped).
- **Code reviewed:** `application/worker/workflows.py`, `application/worker/activities.py`, `application/investigation/{reasoning,verification,planner,context_compiler,memory,validator,disposition}.py`, `application/correlation/engine.py`, `application/evidence/divergence.py`, `domain/hypothesis/models.py`, `domain/investigation/models.py`, `ports/reasoning/reasoner.py`.
- **Recommendations ledger:** every item below was appended as `OPEN` to `docs/IAP_Arch_Eng_Recommendations.log` before this report was written.

## 0. Method and limits (read first)

- **No code was written or modified.** Per instruction, I created no test scripts and made no source edits. The only files touched are this report and the recommendations ledger.
- **What I executed:** the existing offline suites `tests/evaluation/*`, `tests/unit/test_verification_scoring.py`, `tests/unit/test_evidence_bound_verification.py`, `tests/unit/test_reasoning_prompt.py` — 71 passed. These pin the *current* verifier behavior; they do not exercise the three scenarios below.
- **How the three scenarios were "run":** by tracing each scenario through the real code paths (workflow loop → reason → execute → verify → conclude) and reading the existing run records in `docs/IAP-architecture-v1.0.md` (§21–§24, §36, §43). No live LLM, Temporal, Elastic, or Oracle was available, so **LLM-dependent behavior (what the model actually proposes) is inferred from the prompt and forcing rules, not observed.** Findings that depend on model behavior are labeled as such.
- **API verification:** `rustworkx.PyDiGraph.neighbors` was checked against the rustworkx 0.18.1 documentation (returns successors only). The pinned version in `pyproject.toml` is `rustworkx>=0.18.1`.
- **Severity scale:** Critical = the capability does not work at all; Major = intermittent or degraded quality with a non-trivial workaround; Minor = smell or inconsistency; Note = observation.

## 1. Executive Summary

**Decision maturity: guarded, not adaptive.** The platform is a well-fenced single-agent loop. Its strength is *negative* control: citation binding, concession detection, subject grounding, cross-layer provider checks, and an honest-abstention path make fabrication hard. Its weakness is that almost all *positive* reasoning machinery — competing-hypothesis adjudication, contradiction handling, cross-turn memory, replanning, temporal ordering, clarification — is either never written, never read, or never called. The LLM and a stack of substring-triggered forcing rules carry the investigation; the domain model that is supposed to adjudicate between explanations is largely inert.

**Primary architectural vulnerability: the hypothesis/contradiction layer is write-only.** Nothing in `src/` ever writes `refuting_evidence_ids`, `support_score`, `confidence_score`, or `assessments`, or moves a hypothesis to `REJECTED` / `CONTRADICTED`. Every hypothesis formulated in one turn receives identical citations. Ranking therefore degrades to "oldest hypothesis first", and verification fixates on that candidate. Under noisy, multi-cause evidence this is a first-signature-wins system with a strong abstention backstop. It will usually refuse to be wrong, and it will rarely be able to choose between two plausible causes.

**Evidence from the project's own record:** `docs/IAP-architecture-v1.0.md` §43 measured three identical reruns of one question at 0/3 substantive, including one `ANSWERED` conclusion built on a fabricated causal claim from 3 log rows. The architecture doc itself assigns this residual to a "Shape-1 critic" that is "still unbuilt".

**Top three fixes (details in §4):**

1. Make the hypothesis lifecycle real: per-hypothesis evidence assessment, rejection, and contrast-based selection.
2. Replace the global contradiction veto with evidence-linked, severity-graded, resolvable contradictions.
3. Give the reasoner durable cross-turn state (facts, gaps, open questions, observations) and consume `information_gaps` through the replanner.

## 2. Functional Test Observations

### Scenario 1 — Ambiguous / noisy evidence

Test case: database timeout signals overlapping intermittent pod evictions and a false-positive platform alert.

| Step | What the code does | Observation |
| --- | --- | --- |
| Triage | `_classify_failure` (`workflows.py:427-455`) is substring matching. "timeout" / "timed out" yields `TRANSIENT` before BUG or DATA are considered. | The overlapping-signal ticket is classified `TRANSIENT` regardless of the real cause. |
| Plan | `_ROUTE_PLANS["TRANSIENT"] = [SEARCH_LOGS, QUERY_STATE, CONCLUDE]` (`workflows.py:400-406`). | The skeleton ends in `CONCLUDE` and is advisory only (see I-2). The prompt tells the model to favor recent-window logs "over deep code reads" for this route (`reasoning.py:605-612`). If the true cause is a code defect (e.g. connection-pool exhaustion), the route text steers away from it. |
| Forcing | Log-first rule (`workflows.py:593-636`) overrides SEARCH_CODE / QUERY_STATE / CORRELATE until a SEARCH_LOGS banks rows. | Reasonable first move. Keywords derive from complaint nouns. |
| Noise filtering | Evidence ranking is provider best-first, top-10 per query (`activities.py:2383-2388`). Identical masked log shapes collapse to one exemplar (`context_compiler.py:305-347`). | Dedup of repeated lines works. There is no baseline, onset, or frequency comparison, and no notion of "alert" versus "incident". A false-positive alert is just another row. |
| Infra signals | No `ActionType` exists for metrics, events, or Kubernetes (`domain/investigation/models.py:63-88`), although `EvidenceType.METRIC` and `SYSTEM_EVENT` exist. | Pod evictions are only visible if they happen to appear in logs. |
| Hypotheses | Every hypothesis in a FORMULATE batch gets the same `supporting_evidence_ids` (`activities.py:3930-3941`). | "DB timeout" and "pod eviction" are both supported by the same rows. `_prefer_discriminating` (`verification.py:143-186`) finds no unique support and falls back to rank order. |
| Selection | `rank_hypotheses` sorts by status, then `support_score`, then `confidence_score`, then `created_at` (`verification.py:87-101`). Scores are constant 0.5 (never updated). | **The oldest hypothesis wins.** Premature convergence is structural, not just prompt-level. |
| Loop behavior | No recursion hazard: iterations are capped (default 10, clamp 50), forced replans are capped at 2, forcing rules are capped at 2 each. | It does not spin; it exhausts budget on the same candidate and abstains. |
| Contradictions | The model's `contradictions_found` are persisted as `MEDIUM` with no evidence or hypothesis links (`activities.py:1557-1566`). Any unresolved row makes the causal path return `CONTRADICTED` (`verification.py:1127-1141`). Nothing resolves them. | In a noisy case the model is likely to report conflicts, which then veto all causal verification for the rest of the run. |

**Net result:** it does not hallucinate a conclusion on this input and it does not loop. It also cannot arbitrate between co-occurring causes; the outcome is either the earliest-formulated hypothesis or an abstention.

### Scenario 2 — Incomplete evidence

Test case: critical telemetry or queries return empty or fail.

| Step | What the code does | Observation |
| --- | --- | --- |
| Provider absent / error | `ProviderNotConfiguredError` **and** `ExecutionError` both become `DEGRADED` with `miss_reason = "runtime_provider_not_configured"` or `"state_provider_not_configured"` (`activities.py:2346-2360`, `2394-2415`). | A timeout, 5xx, or auth failure is reported to the model as "not configured". Other exception types are not caught and fail the run after 3 Temporal retries. |
| Empty vs absent | `EMPTY`, `DEGRADED`, `BLOCKED` are recorded and returned to the model in `recent_actions` (`reasoning.py:387-407`). | The model can see *that* a query was empty. Nothing distinguishes "valid query, nothing exists" (negative evidence) from "wrong query / wrong window". |
| Time window | `SEARCH_LOGS` builds `RuntimeEvidenceRequest` with keywords and services only — no `time_range` (`activities.py:2383-2388`). The per-iteration `retrieve_evidence_activity` uses `now - default_time_window` (`activities.py:1286-1291`), and its result is discarded (count only). | A ticket investigated hours or days after the incident is searched against the wrong window, or an unbounded one. Empty results then read as absence. |
| Gap handling | `information_gaps` is produced by the model (`ports/reasoning/reasoner.py:21`) and copied into the activity result (`activities.py:1670`). **No code reads it.** `planner.py` (`replan`) has no production caller (only `tests/unit/test_remediation_coverage.py`). | Gaps do not drive the next action, the replan, or the final report. |
| Escalation | The workflow consumes an `input_required` marker (`workflows.py:908`), but **no activity ever produces one** in the reasoning path. | The agent has no mechanism to ask for clarification or escalate missing data. |
| Termination | Unverified runs end `FAILED` in the workflow, then `conclude_investigation_activity` converts `verification_failed_or_limit_reached` / `budget_exceeded` into `COMPLETED` + `INSUFFICIENT_EVIDENCE` (`activities.py:5663-5696`) with a single generic limitation and **empty** `supporting_evidence_ids`. | Honest and safe, but not actionable: it does not say what was tried, what was empty, or what the operator should supply. Banked partial evidence is not attached. |
| LLM outage | `reason_activity` returns `next_action = action_type or "QUERY_STATE"` (`activities.py:1659`) and does not propagate `REASONING_UNAVAILABLE` (it stays in decision metadata). | An LLM outage or invalid-schema streak dispatches default QUERY_STATE turns and ends as a *completed* `INSUFFICIENT_EVIDENCE` — an infrastructure failure recorded as an epistemic result. |

**Net result:** the hallucination guards hold well (concession detection, bound citations, abstention). The platform recognizes "no verified answer" but cannot name the gap, cannot ask for input, and mislabels some failures as absence.

### Scenario 3 — Multi-source correlation

Test case: DB logs, cluster state, platform checks passed to evidence analysis and root-cause verification.

| Step | What the code does | Observation |
| --- | --- | --- |
| What the model sees | `evidence_records` = first 100 rows, summary ≤1000 chars, with id/type/source only (`reasoning.py:338-349`). `compiled_context.evidence_excerpts` adds `observed_at` and `provider` but is sorted by `retrieved_at` (`context_compiler.py:204-216`). | No view is ordered by `observed_at`. `observed_at` is nullable (migration 008), so unknown-time rows mix with timed ones. |
| "Timeline" | `compile_timeline` returns the last 8 workflow state-transition events (`context_compiler.py:423-433`). | This is a platform lifecycle log, not an incident timeline. No component synthesizes a cross-source event sequence. |
| CORRELATE | The graph is built from `evidence_relationships`, whose first in-run producer is the post-conclusion hypothesis→evidence `CORROBORATES` write (`activities.py:5548-5593`). Traversal uses `graph.neighbors(...)` (`correlation/engine.py:173`), which returns **successors only** (rustworkx docs). | Within a run the graph is empty. Even when populated, edges point hypothesis→evidence, so expanding from evidence roots reaches nothing. The repository fetch is bidirectional but the traversal is not. |
| Cross-layer check | Corroboration = `len(providers) >= 2 and len(types) >= 2` plus a row count (`verification.py:1152-1158`). | It proves *independent sources were cited*, not that they *agree*. No discrepancy resolution exists between, say, an application log and a database state row. |
| Causal chain | Causal steps are model-authored; the platform checks only that cited IDs exist in this investigation (`activities.py:3982-4003`, `4454-4466`). Each link is assigned a fixed `confidence=0.7`. | No check that the cited rows actually relate to the cause/effect text, and no check that the cause precedes the effect in time. |
| First-signature risk | Forcing rules fire on the first matching pattern (stack frames, error words). The "independent capability per surface" rule before CONCLUDE exists only as prompt text (`reasoning.py:617-624`). | Prompt-only; the verifier does not enforce it. |

**Net result:** evidence from multiple components is cited together but not reconciled. The platform does not build a coherent timeline and does not cherry-pick a single signature deliberately, but nothing in the code prevents the earliest consistent-sounding story from winning.

## 3. Detailed Gap Matrix

Severity tags follow the calibration in §0. "Lens" maps to the five mandatory review lenses: API = API Surface Accuracy, ARCH = Architecture and Design, DATA = Data and Migration Risk, PROD = Production Readiness, QUAL = Code Quality.

### 3A. Issue Identification (triage, scoping, planning)

#### I-1 Triage is substring matching with precedence that hides the real cause — Major (ARCH, QUAL)

- **The Gap:** `_classify_failure` (`workflows.py:427-455`) maps keywords to a route: transient patterns first, then config (including the bare token `config`), then data (including `missing`, `wrong`, `expected`), then BUG only if frames or exception tokens exist. `question_shape.classify_question_shape` and `classify_surfaces` (`context_compiler.py:87-92`) use the same technique with different vocabularies. Three classifiers disagree by design and none see the others.
- **Operational Impact:** "connection timeout" caused by a code defect is routed `TRANSIENT`, which de-emphasizes code reads and plans a `CONCLUDE`-terminated skeleton. A ticket containing the word "configuration" in prose is routed `CONFIG`. The route also selects the runtime verification preset (`activities.py:921-935`), so misrouting changes the bar.
- **Recommended Architectural Fix:** replace the precedence chain with a scored, multi-label triage that returns a ranked set of candidate routes and evidence-family priors, and record *why* (matched signals). Let the planner issue parallel first probes for the top two routes rather than committing to one. Treat the route as a prior that evidence can overturn after the first banked rows.

#### I-2 The plan is advisory and the real planner is dead code — Major (ARCH, QUAL)

- **The Gap:** `_investigation_plan` (`workflows.py:409-424`) produces a list of up to 5 capability names, appended to history and telemetry only (`workflows.py:1100-1103`, `1228`); nothing checks steps off or deviates from it. `InvestigationPlanner` (`planner.py`: DAG validation, bounded replanning, gap dedupe) has no production caller. `InvestigationState.investigation_plan` and `pending_questions` are never populated (`domain/investigation/models.py:554-559`).
- **Operational Impact:** replanning is reduced to one hard-coded rule — two empties on SEARCH_LOGS or SEARCH_CODE force the other family (`workflows.py:486-541`). There is no plan-level reaction to new clues, no dependency tracking, and no explicit stopping criteria. Replanning in response to `information_gaps` is impossible because the gaps are discarded.
- **Recommended Architectural Fix:** wire the planner into the loop: seed it from triage, let `information_gaps` and refuted hypotheses create steps through `replan`, and have the reasoner see `plan` + `pending_questions` as structured state each turn. Keep `MAX_REVISIONS` and `MAX_STEPS` as the guard rails they already are. Alternatively delete `planner.py` and state that the loop is intentionally reactive — the current half-state is worse than either.

#### I-3 Tool selection is dynamic by prompt, but capability set and forcing rules are rigid and incomplete — Major (ARCH)

- **The Gap:** tool choice is the LLM's, constrained by an ~140-line system prompt (`reasoning.py:497-640`) and eight override rules (`workflows.py:1122-1216`: walk-stack, formulate, synthesis, re-read, log-first, replan). The overrides are ordered, mutually exclusive via flags, each capped at 2 per run, and triggered by keywords. There is no infrastructure/metrics/Kubernetes/alert action despite `EvidenceType.METRIC` and `SYSTEM_EVENT`. The system prompt contains the `allowed_capabilities` instruction twice (`reasoning.py:507-513` and `514-517`).
- **Operational Impact:** the agent is dynamic inside a fixed capability family (code, logs, DB state, API probe, docs). Platform-level incidents (evictions, throttling, deploy events, alert firing history) cannot be investigated directly. Rule interplay is untested as a system: each rule has unit tests, but no test exercises all of them against one noisy run.
- **Recommended Architectural Fix:** (a) add a read-only `GET_PLATFORM_EVENTS` / `QUERY_METRICS` capability family behind the existing provider/authorization model; (b) move forcing rules into a single declarative policy table (condition, override, cap, precedence) that is evaluated in one place and logged as one decision record; (c) add a scenario-level evaluation test that drives all rules in one run.

#### I-4 Premature convergence is structural — Major (ARCH)

- **The Gap:** see D-1 and D-2. Additionally, `verify_root_cause_activity` runs *every* iteration (`workflows.py:1357`), and `if verify_res.data.get("verified"): break` (`workflows.py:1391-1393`) ends the run the moment any single candidate passes. There is no requirement that rivals were considered or eliminated. The prompt asks for "independent capability" per surface (`reasoning.py:617-624`) but the verifier does not enforce it.
- **Operational Impact:** the first hypothesis that clears the bar ends the investigation. With constant 0.5 scores the first one proposed is also the first one verified.
- **Recommended Architectural Fix:** add a "challenge round" gate: before a candidate may conclude, require that at least one named rival has been tested with a discriminating probe, or that the run explicitly records why no rival exists. Enforce in the verifier, not the prompt.

### 3B. Evidence Utilization (processing, correlation, state)

#### E-1 The reasoner is stateless across turns — Major (ARCH, DATA)

- **The Gap:** the model emits `observations`, `information_gaps`, `contradictions_found`, and `reasoning_chain` each turn, but only counts survive: history stores `len(_hyps)` and `len(_obss)` (`workflows.py:1217-1230`); `recent_actions` forwards those integers (`reasoning.py:402-405`). `known_facts` is read into the prompt (`reasoning.py:364-367`) but no code constructs a `Fact` (the only `Fact(` hits are type references). `InvestigationMemory` (`memory.py`) has no caller. Contradictions are persisted but not fed back: the compiled context has no contradictions or gaps section (`context_compiler.py:557-575`).
- **Operational Impact:** the next turn does not know what the previous turn concluded from the evidence, only what rows exist and what hypotheses were saved. Long investigations lose breadcrumbs by design; the 12,000-token budget and tier demotion (`context_compiler.py:460-514`) protect cited rows but cannot protect reasoning that was never stored.
- **Recommended Architectural Fix:** persist a compact per-turn "working notes" record (observation, gap, open question, hypothesis delta, contradiction) as first-class rows, and compile a bounded "what we know / what we lack / what we tried" section into every prompt. Use `InvestigationMemory`'s priority scheme (facts, hypotheses, contradictions, evidence) as the compile order — it already encodes the right priorities.

#### E-2 No temporal reasoning anywhere — Major (ARCH)

- **The Gap:** the investigation time window is `now - default_time_window` at creation (`services.py:168-170`), not the incident time. `SEARCH_LOGS` passes no time range. Evidence is ordered by `retrieved_at`. A repository-wide search for temporal-order logic in `application/` and `domain/` finds none. Causal steps have no precedence check.
- **Operational Impact:** correlation of "DB timeout at T, pod eviction at T-90s" cannot be expressed or verified. Late-run tickets hit the wrong window. A reversed cause/effect passes verification as long as the IDs exist.
- **Recommended Architectural Fix:** carry an explicit incident timestamp (or range) from intake through the investigation context; pass it as the default `time_range` for every runtime query; order every evidence view by `observed_at` with an explicit "time unknown" bucket; add a verifier rule that each causal link's cause evidence is observed at or before its effect evidence (with a configurable skew tolerance) and fails closed when times are unknown.

#### E-3 CORRELATE does not correlate inside a run — Major (ARCH, API)

- **The Gap:** edges are written only at conclusion (`activities.py:5548-5593`, hypothesis→evidence) and by disposition (investigation→investigation). Traversal follows `graph.neighbors` (`correlation/engine.py:173`), which in `PyDiGraph` returns successors only. The relationship fetch is bidirectional, so inbound edges are loaded and then ignored. Result construction does `UUID(node_id_str)` (`engine.py:191`) for every node, but disposition nodes use the `investigation:<uuid>` shape, so a mixed graph could raise `ValueError` (not reproduced; flagged for confirmation).
- **Operational Impact:** CORRELATE is cross-investigation memory at best. No in-run evidence-to-evidence links are extracted (shared trace id, request id, host, entity, time proximity), so the "multi-source synthesis" claim depends on the LLM reading excerpts side by side.
- **Recommended Architectural Fix:** add a deterministic in-run linker that writes evidence↔evidence edges on bank (same trace/request/session id, same entity, time overlap within tolerance); use `neighbors_undirected` (or traverse predecessors and successors explicitly) when expanding; keep node ids typed so non-evidence nodes cannot enter an evidence-UUID result.

#### E-4 "Empty" conflates absence, bad query, and provider failure — Major (PROD, ARCH)

- **The Gap:** see Scenario 2. `ExecutionError` is labeled `*_provider_not_configured`. `retrieve_evidence_activity` runs a keyword-less wildcard query every iteration whose result is dropped — it consumes provider quota and adds latency without informing any decision (`workflows.py:1321-1330`, `activities.py:1286-1330`). Log rows bypass the 25-row evidence budget while all other types share it (`activities.py:32`, `40-44`), so the corpus skews toward logs and squeezes code and state rows that the cross-layer rule needs.
- **Operational Impact:** the model cannot tell "this window has no errors" from "my search failed". A Kubernetes or Elastic outage looks like evidence of health. Budget behavior biases against the cross-layer evidence the verifier demands.
- **Recommended Architectural Fix:** a typed outcome taxonomy (`NO_MATCH_VALID_QUERY`, `QUERY_REJECTED`, `PROVIDER_ERROR`, `PROVIDER_ABSENT`, `DENIED`, `TIMEOUT`) with distinct `miss_reason` values; only `NO_MATCH_VALID_QUERY` may count as negative evidence, and it should be banked as a citable absence row bound to the exact query and window. Remove or repurpose the per-iteration wildcard retrieval. Budget per evidence family, not only logs-versus-rest.

#### E-5 Evidence summaries are truncated before the model sees them, with a heuristic relevance — Minor (QUAL)

- **The Gap:** `evidence_records` truncates summaries to 1,000 characters and carries a `truncated` flag (`reasoning.py:338-349`). `relevance` defaults to 1.0 (`context_compiler.py:266`, no producer found in `application/`), so Tier-1/Tier-2 decisions rely on recency, which is `retrieved_at` rather than `observed_at`.
- **Operational Impact:** the oldest-by-retrieval evidence is demoted first even when it is the earliest and most causal observation.
- **Recommended Architectural Fix:** compute relevance from the active hypotheses and open questions (not a default), and demote by lowest hypothesis-relevance rather than oldest retrieval.

### 3C. Decision and Conclusion (reasoning, confidence, actionability)

#### D-1 The hypothesis lifecycle is write-only; competing explanations are never adjudicated — **Critical** (ARCH, DATA)

- **The Gap:** `grep` across `src/` finds no writer for `refuting_evidence_ids`, `support_score`, `confidence_score`, or `assessments`, and no transition to `HypothesisStatus.REJECTED` or `CONTRADICTED` (only reprocess copying reads them). `HypothesisEvidenceAssessment`, `AssessmentType`, and `AssessmentStrength` are defined and persisted but never produced. FORMULATE saves each statement with the *same* cited ID list (`activities.py:3930-3941`) and dedupes only on exact statement text, so paraphrases accumulate until the total cap of `max_hypotheses` (default 10) and then `allowed = 0` (`activities.py:3899`) blocks any new hypothesis for the rest of the run.
- **Operational Impact:** `rank_hypotheses` collapses to creation order. The `refuting` branches in verify and conclude (`activities.py:4351`, `5338`) can never fire. Rejected-alternative bookkeeping, ranked alternatives, and "against evidence" cannot reflect real evidence. A poor early hypothesis set permanently consumes the hypothesis budget.
- **Recommended Architectural Fix:** make "assess" a first-class step. After each evidence-banking action, run a bounded assessment pass that, for every open hypothesis, records SUPPORTS / CONTRADICTS / NEUTRAL with strength and a reason per new evidence row (model-proposed, platform-validated against IDs), updates `support_score` / `refuting_evidence_ids`, and transitions hypotheses to `CONTRADICTED` / `REJECTED` by rule. Select the verification candidate by *discriminating* support (evidence that supports it and refutes rivals), not by creation order. Bind citations per hypothesis, not per batch.

#### D-2 Verification fixates on one candidate and never learns from a failed attempt — Major (ARCH)

- **The Gap:** `select_verifiable_candidate` returns the same top-ranked candidate on every iteration (`verification.py:104-186`, `activities.py:4322`); a failed verification only moves `PROPOSED → UNDER_INVESTIGATION` (`activities.py:4524-4526`) and never excludes it.
- **Operational Impact:** after one INCONCLUSIVE result the loop keeps re-testing the same claim while other hypotheses are never evaluated, until the iteration cap.
- **Recommended Architectural Fix:** record each verification verdict against the hypothesis (verdict, blockers, iteration) and make blockers part of the next prompt ("H1 failed: no cross-layer evidence; gather X"). Rotate the candidate when the same blockers repeat, and surface the blockers to the planner as new steps.

#### D-3 Contradictions are a global, unresolvable veto — Major (ARCH, DATA)

- **The Gap:** contradictions come only from the model's own `contradictions_found` strings, are saved as `MEDIUM`, with empty `evidence_ids` and `hypothesis_ids` (`activities.py:1557-1566`), and are never resolved — `resolve()` and `accept_risk()` have no callers outside `verification.py`. On the causal path *any* unresolved row yields `CONTRADICTED` (`verification.py:1127-1141`); on the location/doc/state fast paths only `HIGH`/`CRITICAL` rows block (`verification.py:791-800` etc.), and nothing ever creates `HIGH` or `CRITICAL`. The `ConclusionGate` blocker for `HIGH`/`CRITICAL` (`verification.py:1278-1286`) is therefore dead in production, and the `against_evidence` field for "likely" conclusions (`verification.py:1196-1225`) can never be populated on the causal path, because reaching it requires zero unresolved rows.
- **Operational Impact:** the model is penalized for reporting conflicts (one report vetoes causal verification permanently) but is never penalized for omitting them. Conflicting sources are never reconciled, and "likely" conclusions never carry counter-evidence.
- **Recommended Architectural Fix:** model contradictions as evidence-linked pairs (rows A and B disagree about X), scoped to the hypotheses they affect. Grade severity from the *affected* hypothesis and the rows' provider reliability and recency, not a constant. Add a resolution step (a targeted probe or a tie-break rule) and let contradictions that do not touch the leading hypothesis surface as `against_evidence` instead of blocking.

#### D-4 Confidence is count-based and cannot express uncertainty between causes — Major (ARCH, QUAL)

- **The Gap:** `overall = 0.35·coverage + 0.25·reliability + 0.20·causal + 0.20·(1 − contradiction_penalty)` (`verification.py:203-211`). `coverage = min(1, n_items/4)` when cross-layer (`1166-1170`), `reliability` is the fraction of rows with *any* provenance object (`1171-1174`, effectively 1.0), `causal = min(1, steps/2)` (`1175`), and the contradiction penalty is passed as 0.0 on every non-contradicted path, so 0.20 is granted for free. Each causal link is hard-coded at `confidence=0.7` (`activities.py:4459`). The existing test `test_two_independent_layers_verify` shows the engine alone returns VERIFIED for 8 rows across two providers with an empty causal chain; only the activity-layer blocker ("no validated causal chain") prevents that in production, and the legacy mode (`IAP_VERIFIER_V2=legacy`) removes it. Location/doc/state answers are fixed at 0.90 (`verification.py:860-865`).
- **Operational Impact:** confidence rises with the number of cited rows and causal steps the model chooses to emit, not with discriminating power. The "established" floor (0.90) and the one-causal-step score are close enough that tier assignment is sensitive to small scoring changes (arithmetic not executed in this review; worth a boundary test). Verified conclusions can be right for the wrong reasons, as the §43 variance run shows.
- **Recommended Architectural Fix:** derive confidence from contrast: evidence that supports the leader *and* fails to support or refutes each rival, penalized by untested rivals, unresolved gaps, stale or time-unknown rows, and single-provider dependence. Keep the existing deterministic gates as vetoes. Build the Shape-1 critic the architecture doc already scopes, with fan-out and an overturn rule, and use `overconfident_turns` (already instrumented at `workflows.py:1395-1407`) as the rollout metric.

#### D-5 Infrastructure failure is recorded as epistemic abstention — Major (PROD)

- **The Gap:** `ReasoningCoordinator` returns `REASONING_UNAVAILABLE` on gateway error, empty response, or invalid schema (`reasoning.py:263-271`, `645-679`), but `reason_activity` does not surface it and defaults the action to `QUERY_STATE` (`activities.py:1659`). The workflow keeps looping, and `conclude_investigation_activity` persists `COMPLETED` + `INSUFFICIENT_EVIDENCE` for the "unverified within budget" ending (`activities.py:5663-5696`). Also, `readiness >= 0.95` breaks the loop on the model's own self-report (`workflows.py:1410-1412`) and then the run ends as an abstention unless verified.
- **Operational Impact:** an LLM outage produces "completed" abstentions that feed disposition (`ABSTENTION_CONCLUSIONS`, `disposition.py`), clustering, and the scoreboard as if they were genuine investigations. A confident but unverified model stops the run early.
- **Recommended Architectural Fix:** propagate `REASONING_UNAVAILABLE` as a typed activity outcome; after N consecutive unavailable turns, end the run `FAILED` with a distinct failure code and do not write an abstention conclusion. Remove the readiness-based break or require it to coincide with a verification attempt.

#### D-6 Findings are not actionable and abstentions drop what was learned — Major (QUAL, PROD)

- **The Gap:** conclusions carry `root_cause` text, `best_reason`, `ranked_alternatives` (status plus statement only, `activities.py:5241-5262`, `5497-5517`), and limitations. There is no remediation field, no verification step ("how to confirm the fix"), and no owner or component. Abstention conclusions store `supporting_evidence_ids=[]`, `contradicting_evidence_ids=[]`, and a single generic limitation string; the `_persist_abstention` path (`activities.py:5208-5239`) records blockers but not `information_gaps`, queries tried, or empty/failed sources. `InvestigationActionValidator` (`validator.py`) has no production call site (tests only), and `input_required` has no producer, so neither parameter validation nor operator clarification is part of the loop.
- **Operational Impact:** an operator reading `INSUFFICIENT_EVIDENCE` learns nothing about what to do next. A verified root cause stops at diagnosis.
- **Recommended Architectural Fix:** extend the conclusion schema with `gaps` (typed, from `information_gaps` plus empty/failed sources), `attempted` (action, params digest, outcome), `recommended_next_probes`, and, for verified causes, `remediation_hypothesis` + `verification_check` with evidence-linked rationale. Emit `input_required` when a gap names data only the operator can supply. Decide whether `InvestigationActionValidator` is the enforcement point and wire it in, or remove it so the security story is not misleading.

#### D-7 Prompt-borne policy is large and duplicated; behavior depends on model compliance — Minor (QUAL)

- **The Gap:** the system prompt is a single ~140-line string inside `reasoning.py`, with repeated sentences (the capability-list instruction appears twice) and policy that is also encoded as forcing rules, validator rules, and verifier rules. `REASONER_PROMPT_VERSION` is stamped (`context_compiler.py:23`), which is good, but the prompt is not separable from the code for evaluation.
- **Operational Impact:** drift between the prompt, the forcing rules, and the verifier is likely; a change to one will silently contradict another.
- **Recommended Architectural Fix:** externalize the prompt into versioned sections, one per policy, each with a named invariant and the test that asserts it; generate the duplicated capability list from the profile at runtime only.

## 3D. Cross-check

**Findings that recur across lenses (highest confidence):**

| Theme | Gaps | Lenses |
| --- | --- | --- |
| Hypothesis and contradiction machinery never exercised | D-1, D-2, D-3, I-4 | ARCH, DATA, QUAL |
| Information produced but dropped (gaps, observations, contradiction text, relevance) | E-1, I-2, D-6, E-5 | ARCH, DATA, QUAL |
| Absence vs failure vs bad query not distinguished | E-4, D-5, Scenario 2 | PROD, ARCH |
| Dead or unwired components (`planner.py`, `memory.py`, validator, `input_required`) | I-2, E-1, D-6 | QUAL, ARCH |
| No time model | E-2, E-3 | ARCH, API |

**Lens coverage:**

- **API Surface Accuracy** produced the fewest findings (E-3 only, plus the rustworkx check). This reflects scope, not cleanliness: the prompt scoped the review to behavior, and I verified only the one third-party call that affects reasoning outcomes. LLM provider adapters (`infrastructure/reasoning/*`), Temporal SDK usage, and Elastic/Oracle client calls were **not** reviewed and should not be read as passed.
- **Data and Migration Risk** is thin (D-1 persisted-but-unused columns, D-6 abstention data loss). The 20+ migrations were not examined; the finding is limited to what the reasoning layer persists.
- **Production Readiness** and **Code Quality** are well represented because the behavior gaps mostly show up as operational and dead-code issues.

## 3E. What's Good

- **Fail-closed verification design.** Concession detection, citation binding, range-read requirement for definition claims, subject grounding, and the cross-layer provider-independence rule (F-050) directly address earlier fabrication incidents, and each has a regression test and a recorded origin run.
- **Honest abstention is a first-class outcome**, with ranked alternatives preserved. It is the right default for an autonomous system.
- **Bounded autonomy.** Iteration, evidence, hypothesis, token, and tool-call budgets; capped forcing rules; capped replans. Nothing in the loop can run away.
- **Operational discipline.** Outcome typing (`SUCCEEDED/EMPTY/DEGRADED/BLOCKED`), per-turn history fed back to the model, forced-override visibility in timeline descriptions, prompt version and hash stamped per decision, `overconfident_turns` instrumentation, and an incident-replay evaluation suite.
- **Self-aware roadmap.** The architecture doc already names the planner, contrast elimination, and critic as the open work; this review mostly confirms and sharpens that list.

## 3F. Out-of-scope observation worth a separate ticket

`InvestigationActionValidator.validate_action` (path-traversal, SQL-token, and bounds checks) is never called from production code; only `tests/unit/test_core_coverage.py` calls it. If the intent was defense in depth for agent-proposed parameters, the loop currently relies on downstream adapters. Confirm where each check is enforced in the execute path before relying on it.

## 4. Prioritized Recommendations

### 1. Make hypothesis adjudication real (closes D-1, D-2, I-4; partially D-4)

Add an evidence-assessment step after every banking action, per-hypothesis citations, rule-based rejection, discriminating-support candidate selection, and a "challenge round" gate that requires at least one tested rival before conclusion. This is the largest quality lever because it turns first-signature-wins into evidence-driven elimination, and it activates code that already exists (`refuting_evidence_ids`, assessments, ranked alternatives, `against_evidence`). **Verify with:** a scenario test where two hypotheses share early evidence and a later row refutes one; expected outcome is that the refuted one is rejected and the survivor is selected regardless of creation order.

### 2. Replace the global contradiction veto with evidence-linked, resolvable contradictions, and stop recording infrastructure failure as abstention (closes D-3, D-5, E-4)

Contradictions become pairs of evidence rows scoped to hypotheses, graded and resolvable; `REASONING_UNAVAILABLE` and provider errors get their own typed outcomes and failure codes; empty results are split into valid-negative versus failed. This removes the two failure modes that most directly corrupt the corpus the rest of the system learns from (clusters, cached dispositions, scoreboards). **Verify with:** (a) a run where the model reports one conflict yet a rival with clean support still verifies; (b) an LLM-outage run that ends `FAILED` with a distinct code, not `INSUFFICIENT_EVIDENCE`.

### 3. Give the agent durable working memory and a real planner, with an incident-time anchor (closes E-1, E-2, E-3, I-2, D-6)

Persist per-turn observations, gaps, and open questions; compile them into every prompt; wire `information_gaps` into `InvestigationPlanner.replan`; carry an incident timestamp from intake into every runtime query; order evidence by `observed_at`; add in-run evidence-to-evidence links and a precedence check on causal steps; emit `input_required` for gaps only an operator can fill; and extend conclusions with attempted probes, gaps, and next steps. This is what makes multi-source correlation and long investigations possible, and it makes abstentions actionable. **Verify with:** a three-source replay (DB log, cluster event, alert) with out-of-order retrieval; expected outcome is a timeline ordered by observation time and a causal link rejected when the cause is observed after the effect.

### Sequencing note

Do 1 and 2 first and together: they are small relative to their effect, touch mostly existing structures, and unblock meaningful measurement. Re-run the §43 variance protocol (same question ×3 via `RESTART_FRESH`) after 1 and 2 and use `overconfident_turns` to decide how much of the Shape-1 critic is still needed.

## 5. Not verified / needs confirmation

- Whether the model, in practice, emits `contradictions_found` often enough to hit D-3 (inferred from the prompt and schema; needs a live trace).
- The `UUID(node_id_str)` failure on non-evidence nodes in `EphemeralCorrelationEngine` (E-3) — read, not reproduced.
- Exact tier-boundary arithmetic of the confidence formula at the "established" floor (D-4) — flagged, not executed.
- Everything in `infrastructure/`, provider adapters, the MCP surface, migrations, and the knowledge/hub orientation path (`hub_orientation`, Cognee recall) beyond how their outputs enter the prompt.

## 6. Summary

| Severity | Count | Items |
| --- | --- | --- |
| Critical | 1 | D-1 |
| Major | 13 | I-1, I-2, I-3, I-4, E-1, E-2, E-3, E-4, D-2, D-3, D-4, D-5, D-6 |
| Minor | 2 | E-5, D-7 |

Total findings: 16 (1 Critical, 13 Major, 2 Minor).

**Top three to fix first:** (1) hypothesis adjudication, (2) contradiction model plus failure-vs-abstention typing, (3) durable working memory with planner and time anchor.
