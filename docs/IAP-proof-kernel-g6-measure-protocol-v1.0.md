# G6-Measure Operator Protocol v1.0 — Shadow-vs-Live on Real Runs

> Status: operator runbook. Capture plumbing (Step 1, option A) was
> authorized 2026-10-24 and implemented — `application/investigation/shadow_capture.py`
> (pure builders + replay), per-turn hook in `application/worker/workflows.py`,
> trace/summary through `loop_telemetry` + `_derive_loop_telemetry`
> allowlist, 8 unit tests, full suite green, no behavior change. Exit:
> G6 PASS declared by the gate owner, or HOLD with named gaps.
> Preconditions: WP-D2-pure + G6-harness unit half green (done);
> live runs available to the operator.

## 0. What is (and is not) measured

G6 asks whether the shadow planner earns its keep: deterministic replay
+ divergence/budget/recovery on **real runs**, plus measurable
action-quality or diagnostic benefit, with zero side effects. The unit
half (`application/investigation/replay.py`, 11 tests) proves the
machinery on fixtures; this protocol puts real histories through it.

Measured now (harness-supported): **choice divergence** (recorded vs
shadow-selected action), terminal agreement, candidate-set changes —
`diff_decisions()` vocabulary, bounded causes.

Deferred to the verdict decision (§4): **predicted discriminator,
evidence novelty, proof progress, cost**. These ADR-005 dimensions have
no unit-harness representation yet (`PlannerSnapshot`/`PolicyDecision`
carry no discriminator, novelty, progress, or cost fields). The protocol
records what is observable per dimension and names what isn't, rather
than claiming unmeasured coverage.

## 1. Capture: how real histories become replayable (decision required)

### Option A — shadow-computation hook (recommended, needs owner auth; authorized and implemented 2026-10-24)

As-built note 2026-10-24: the implemented hook has no capture flag and runs unconditionally under the Step-1 authorization; there is no flag-off run mode, so flag-off equivalence for G6 capture is open (tracked in docs/IAP-CurrentGaps-Fixes-impl-plan.md).

At each loop turn, beside (never inside) the forcing cascade, build the
frozen `PlannerSnapshot` from already-available loop state, run
`decide()`, and append `(snapshot, recorded_action, recorded_outcome,
shadow_selected)` to a diagnostics ledger. Shadow output never feeds
routing, never persists to authoritative rows, never alters control
flow — compute + log only. Small, reviewable diff confined to the
workflow module; covered by a dedicated no-behavior-change test
(flag-on/flag-off run equivalence on goldens, same shape as D1-G).

### Option B — offline reconstruction (no code change, lossy)

Rebuild snapshots from run telemetry (timeline entries, history
outcomes, banked-evidence counts). Lossy where telemetry lacks trigger
state (`value_target_present`, `stack_walk_owed`, per-rule forcing
counts are often not recorded). Any divergence measured on
reconstructed snapshots must be labeled `reconstructed-input` and
cannot fail the gate alone — it can only motivate Option A.

**Recommendation**: authorize A. B is admissible as a pilot on 2–3 runs
to confirm the ledger shape, not as gate evidence.

## 2. Measurement (operator executes)

For each instrumented run (minimum viable sample set by operator
judgment; suggested floor: 20 runs spanning PASS/PARTIAL/FAIL outcomes):

1. **Replay**: feed recorded `(snapshot, recorded_action)` turns through
   `replay_history()`. Aggregate with `summarize_trace()` (eligible /
   diverged semantics per §4.2). Record per-run: total turns, eligible
   turns, matches, divergences with causes, terminal agreements.
2. **Budget**: per run, actions_used vs action_limit and token usage at
   terminal turns; count turns where the shadow would have concluded
   earlier than the loop did (`shadow-terminal` divergences) and turns
   where the loop concluded but the shadow would have continued.
3. **Recovery**: turns following FAILED/BLOCKED outcomes — did the loop
   avoid blind retry (agreeing with the shadow's withhold/penalize), retry
   blindly (divergence with cause), or retry with a documented relevant
   change (not blind per the frozen §4.2 definition)? Tally all three.
   Each stamped turn carries `evidence_banked_total` (cumulative, for
   delta computation), `prev_outcome`, and capped `params`, so the
   classification is computable offline from the ledger alone.
4. **Zero-side-effects re-verification**: confirm the instrumented loop
   produced identical rows, verdicts, and routing vs the uninstrumented
   baseline on the same fixtures (the D1-G method, applied to the hook).

## 3. Report shape (operator returns)

```text
runs measured: N (ids or redacted refs)
turns replayed: T, matched: M, diverged: D (divergence rate D/T)
divergence causes: {forcing-rule: n, recorded-withheld: m, shadow-terminal: k, unmodeled-loop-forcing: u, rank-disagreement: j}
early-conclude opportunities: k turns | late-conclude: l turns
recovery agreement: x/y post-failure turns
baseline equivalence: identical outputs yes/no (diff attached if no)
```

No tenant keys, no payload contents, no secrets — counts and verdicts
only (same handling rule as the G5 verdict).

## 4. Gate decision (adopted 2026-10-24; thresholds review)

Passing these bars qualifies the shadow-measurement evidence for the
next gate — it does **not** authorize the shadow to influence
authoritative decisions, enable typed-edge consumption, or satisfy
publication. G5-formal and D3 rollout requirements remain separate.
Thresholds are initial policy, revisable only by explicit owner
decision after sufficient run data — never tuned retrospectively to
make a run-set pass.

### 4.1 Bars and decision rules

| # | Quantity | Decision |
|---|---|---|
| 1 | Divergence rate `D/T` | PASS candidate: below 15%. INVESTIGATE: 15–30% inclusive. HOLD: above 30% in **two consecutive eligible run-sets** (one set above 30% triggers immediate investigation but not HOLD). Below 15% is candidacy, not sufficiency — all other bars still apply. |
| 2 | Explained divergence share | PASS: ≥70% of divergences carry a validated forcing/withheld/terminal cause. Otherwise investigate; withhold the gate decision. Zero divergences: N/A (the rate bar carries the signal). Report unexplained count alongside the share. |
| 3 | Recovery agreement | PASS: ≥80% of eligible post-`FAILED`/`BLOCKED` turns agree. Otherwise investigate; withhold the gate decision. No eligible turns: N/A **and** insufficient evidence for this bar — never inferred from absence. |
| 4 | Baseline equivalence | PASS: exact. HOLD: any unexplained or contractually forbidden difference. No tolerance. A telemetry-only exception must be defined in advance, never created after a failed comparison. |
| 5 | Minimum sample | ≥20 eligible runs spanning both PASS and FAIL outcomes. Below: INSUFFICIENT EVIDENCE, not PASS. Exact counts published; 20 is a floor, not a stability claim — near-threshold or varying results require more data. |
| 6 | Capture health | `capture_errors == 0`. Any capture error: HOLD the run-set until the missing turn is recovered or the set is rerun. |

### 4.2 Operational definitions (frozen before measurement)

- **Eligible turn** (`T`): a turn for which both the recorded outcome
  and the shadow outcome are available and comparable under the frozen
  comparison rule. Missing/invalid observations affect capture health
  or eligibility — never silently dropped to improve the rate. `D`:
  turns where shadow differs from recorded. `D/T` is measured only
  after capture health and baseline equivalence pass.
- **Explained divergence**: cause from the frozen vocabulary
  (forcing-rule, recorded-withheld, shadow-terminal, rank-disagreement
  per the harness), supported by recorded evidence. Generic labels and
  post-hoc explanations do not count. One further explained category:
  **unmodeled-loop-forcing** — the turn's recorded `forced` list
  contains a loop rule outside the shadow's seven-rule vocabulary
  (`log_search`, `replan`): the loop was compelled by a rule the shadow
  cannot model, so the disagreement evidences a vocabulary gap, not a
  routing error. Counted separately from shadow-modeled forcing
  agreement; a high unmodeled share is a design finding (extend the
  shadow vocabulary vs accept the blind spot), never a quality failure
  of either side. All other divergences (`rank-disagreement` with no
  forcing on either side) are unexplained.
- **Blind retry** (frozen): a retry without new evidence, a changed
  input, a changed execution context, or a specific remediation. A
  retry with a documented, relevant change is not blind. Agreement
  counts only when loop behavior and shadow no-retry discipline concur
  under this definition. Failures classify as reckless retries,
  justified retries incorrectly withheld, or other disagreement.
  Numerator and denominator reported, never the percentage alone.

### 4.3 Overall gate decision (single status, precedence-ordered)

- **PASS** — eligible for the next review gate: sample + capture-health
  pass; baseline exact; divergence below 15%; explained share ≥70%
  when applicable; recovery ≥80% when applicable.
- **INVESTIGATE** — divergence 15–30%, an explanatory/recovery bar
  missed, or run-sets vary too much for a stable interpretation.
- **HOLD** — baseline differs, capture errors nonzero, or divergence
  above 30% in two consecutive eligible run-sets. Do not promote on
  the strength of the other metrics.
- **INSUFFICIENT EVIDENCE** — fewer than 20 eligible runs, missing
  PASS/FAIL coverage, or no eligible recovery turns for bar 3. Collect
  more data; missing evidence is never success.

Precedence: HOLD overrides INVESTIGATE, INVESTIGATE overrides PASS.
An N/A metric is acceptable only on a genuinely zero denominator with
the reason reported; it waives nothing else.

### 4.4 Report shape (per run-set, gate-driving vs diagnostic split)

Publish: run-set id + flag/configuration versions; eligible run/turn
counts with PASS/FAIL coverage; `D`, `T`, `D/T`; explained and
unexplained divergence counts; recovery numerator + denominator;
capture errors; baseline-equivalence result; final status, failed
criteria, investigation refs. Diagnostics (latency, cost, limitation
distribution, per-resolver splits, divergence subtypes) ride along
clearly separated — never promotion criteria without an independently
justified safety requirement.

## 5. Known residuals (not closable by this protocol)

- ADR-005's discriminator/novelty/progress/cost dimensions need model
  + harness extensions before they are measurable; this protocol must
  not be cited as covering them.
- Parser-execution D1-B vectors (real Tree-sitter conformance) remain
  OPEN alongside.
- D3-family rollout needs G6 PASS **plus** per-family parity evidence;
  this protocol is the key, not the whole door.
