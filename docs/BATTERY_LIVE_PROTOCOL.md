# Battery Live-Run Protocol (Phase 4.3)

This protocol runs the offline-graded battery against a live stack. The grader (`scripts/tranche_b_battery.py`, fixtures in `tests/fixtures/battery_cases/`, offline suite `tests/unit/test_battery_scoring.py`) is authoritative on rubric semantics; this document covers only live execution: environment, repetition, recording, and pass criteria.

## 1. Environment

- Deploy the checkpoint under test with a fresh database (no prior investigations for the battery tenant). Record the commit SHA, model name, temperature, and prompt version (`REASONER_PROMPT_VERSION`) before starting — all four go into the run record.
- Services required: API, Temporal worker, code-intelligence provider over the sample checkout (`scripts/build-sample-checkout.py`), state provider. The OTel collector is optional; if absent, counter assertions are skipped (grader verdicts do not depend on them).
- Live-runnable cases only: the runner skips fixtures with `live: false` and reports them as SKIP. Never force a skipped case live.

## 2. Execution

- Run `uv run python scripts/tranche_b_battery.py [--only L1,...]` exactly 3 times per case (variance measurement — a single pass is not evidence of reliability). Use `--only` to shard long runs; the scoreboard aggregates by case ID.
- Between runs, reset the battery tenant's investigations (or use a fresh tenant per run) so lineage/merge behavior cannot leak evidence across runs.
- Do not retry failed cases ad hoc. A timeout or FAILED run is data: record it, continue the sweep, investigate after.

## 3. Recording

- Keep the runner's printed scoreboard (case, verdict, detail, investigation ID) as the run record. For every non-PASS, attach the conclusion payload (`GET /api/v1/investigations/{id}/conclusion`) and the failing rubric dimension from the detail string.
- Record guardrail endpoints alongside accuracy: abstention rate, median turns to decisive read, cost per supported claim (see DS_REVIEW §13.3). A treatment that improves location answers while making causal answers confidently wrong is a regression — stratify results by claim kind before aggregating.

## 4. Pass criteria

- Exit code is nonzero on any FAIL or PARTIAL — PARTIAL never counts as pass (a right answer on wrong evidence is a grading failure, not a near-miss).
- Paired comparison for treatments (prompt/context/verifier changes): same cases, same model, same temperature, same 3 repetitions. Primary endpoint is `code_claim_support_rate`; guardrail endpoints must not regress beyond agreed bands.
- Eval-set hygiene: hold out a subset of fixtures from iteration, include anonymized real incident traces before calling a treatment production-ready, and re-run the offline grader suite (`tests/unit/test_battery_scoring.py`) on every change — rubric drift breaks the live comparison silently.
