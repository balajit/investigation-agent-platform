# Problem Definition 1/3 — L2 Variance Diagnosis (READ-ONLY)

> Branch context: `L2-D3-fix` (branched from `iap-onboarding-v0.1` @ `5a7eb13`).
> This prompt hands the L2-D3-fix problem to GPT for a first shot.
> **Hard constraint: read-only. No code changes, no migrations, no live runs,
> no flag changes, no commits.** Diagnosis only — name the mechanism.

## 1. Background

Battery case L2 (`tests/fixtures/battery_cases/L2.json`) asks the callers
question:

```text
symptom: "What calls the order service handler?"
expect:  status=ANSWERED, claim=callers, callee=OrderServiceHandler,
         callee_method=handle, expect_any=[OrderQualifyHelper, OrderServiceRestProxy]
```

The same L2 case flips PASS/FAIL across identical arms and configurations.
Owner classification (2026-10-24, ledger line 501): the L2 divergence is
**variance, not interference** — but the mechanism is unnamed. Naming it is
this task.

## 2. Observed evidence (do not re-run; read these)

| Run set / arm | L2 verdict | Detail |
|---|---|---|
| r1 shadow (`reports/d3-stagec-2026-10-24-r1-shadow.md`) | PASS | COMPLETED, conclusion supported by cited evidence, 15.1s, 3 ev / 2 hyps |
| r2 shadow (`reports/d3-stagec-2026-10-24-r2-shadow.md`) | **FAIL** | COMPLETED, `status=INSUFFICIENT_EVIDENCE, want answered`, 45.2s, 10 ev / 2 hyps |
| r2 control, flag-off (`reports/d3-stagec-2026-10-24-r2-control.md`) | PASS | COMPLETED, conclusion supported by cited evidence, 15.1s, 4 ev / 1 hyp |
| Scratch batteries `dev-shadow-certify-full*.md`, `dev-shadow-certify-L1*.md` | PASS (all) | Same flag-ON configuration as the r2-shadow FAIL |

Key rows (retained in dev DB, do not modify):
- r2-shadow FAIL: investigation `e5392dd6` (16-trace struggle run, 45.2s)
- r2-control PASS: investigation `5469c121` (fresh flag-off run, 15.1s)
- r1-shadow PASS: same symptom, tenant `iap-probe-2026-10-24`
- Freeze context: `docs/IAP-proof-kernel-d3-stageC-freeze-record-v2.md`
  (record hash `ff0cf8e7…`), run set `d3-stagec-2026-10-24-r2`,
  control artifact `artifacts/d3-stagec-2026-10-24-r2/control-artifact.json`
  (pairing verdict for L2: `DIVERGE-STOP`; read its L2 `divergence` block —
  it already records the leading variance hypothesis and history)

## 3. Task for GPT

1. Read the L2 rows above (reports + retained DB rows via read-only queries;
   per-turn verification outcomes, evidence banked per turn, hypotheses and
   their support, conclusion rationale).
2. Compare the FAIL run (`e5392dd6`) against the PASS runs, turn by turn:
   where does the FAIL trajectory first depart (retrieval miss vs.
   formulation miss vs. verification demotion vs. conclude gate)?
3. Name **one** primary mechanism (e.g. caller-enumeration grounding,
   rival-caller handling, evidence-window truncation, reasoner sampling
   variance, serve_cached/duplicate lineage) with file:line evidence, and
   state what would falsify it.
4. Classify: deterministic defect (fixable), sampling variance (needs
   convergence budget or retry/aggregate policy), or fixture/expectation
   problem. If variance, say what measurement would confirm it
   (e.g. N repeats, pass-rate confidence interval) — do not run it.

## 4. Frozen constraints (do not violate)

- No changes to `src/`, `tests/`, `config/`, migrations, flags, or env.
- No live runs, no worker restarts, no tenant writes; read-only DB queries only.
- No commits. No references to `tmp/` scratch in any output.
- The r2 freeze (`ff0cf8e7…`) and r2 verdict (INSUFFICIENT, kept open)
  stand; this diagnosis must not reinterpret them.

## 5. Acceptance criteria

- [ ] A single named mechanism with file:line evidence and a stated falsifier.
- [ ] Turn-by-turn divergence point identified (run e5392dd6 vs a PASS run).
- [ ] Classification: defect vs. variance vs. fixture problem, with the
      follow-up it implies (concrete fix sketch, measurement plan, or
      fixture revision — sketch only, no code).
- [ ] Output as a short report (this file may gain an appendix; do not
      create new top-level docs without asking).

## 6. Non-goals

- No fix implementation. No new tests. No re-freeze, no new run set.
- No verdict changes to r1/r2 records.
