# L2-D3-Fix: Thought Process, Test Cases, and Runtime Test/Report Procedure

> Branch context: `L2-D3-fix` (branched from `iap-onboarding-v0.1` @ `5a7eb13`).
> Companion prompts: `docs/IAP-L2-variance-diagnosis-problem.md` (Item 1),
> `docs/IAP-D3-definition-convergence-problem.md` (Item 2),
> `docs/IAP-D3-remaining-families-problem.md` (Item 3).
> **Docs only. No new code, no new scripts** — the procedure below uses only
> existing scripts and read-only queries.

## 1. Thought process

1. **L2 first because it is the only open empirical question gating the
   definition family.** The r2 run set stopped per §6.2g on the L2 native
   divergence (shadow FAIL vs flag-off control PASS); the owner classified
   it as variance, not interference, and ruled the run set INSUFFICIENT
   (kept open). Until the flip mechanism is named, any fix — and any new
   formal collection — would be gambling against an unknown.
2. **Read-only diagnosis precedes any fix.** The retained rows (r1/r2
   tenants) already contain the full natural experiment: 5 PASS flag-ON,
   1 FAIL flag-ON (45s/16-trace struggle), 1 PASS flag-OFF. If the
   mechanism is visible anywhere, it is in the turn-by-turn divergence
   between run `e5392dd6` (FAIL) and any PASS run — no new runs are
   needed to name it, and new runs are forbidden until it is named.
3. **The r2 stop-rule precedent shapes everything after.** Even under a
   variance classification, a fresh native divergence during future
   collection stops the probe again. So the fix (or convergence proof)
   must make L2 *stably* ANSWERED — a second "it passed this time" is
   not convergence. The procedure below therefore demands repeated
   shadow runs plus a flag-off control before anyone claims the flip
   is gone.
4. **What would overturn the variance classification:** a second FAIL
   whose trace shows shadow-path causation (e.g. tap/observer/pairing
   errors, shadow-induced native path change, flag-dependent behavior
   difference with all else equal) rather than the known struggle-run
   signature. A single additional data point of that shape reopens the
   interference question and voids any convergence claim.

## 2. Environment, database, and credentials (read this first)

- **Env files (layered in this order; later overrides earlier):**
  1. `./.env.local` (repo root; tracked, dev defaults)
  2. `~/bin/.iap.env.local` (operator overrides)
  
  Layering commands (exactly as `scripts/run_battery.sh` does it):
  ```bash
  [[ -f ./.env.local ]] && source ./.env.local
  [[ -f ~/bin/.iap.env.local ]] && source ~/bin/.iap.env.local
  ```
- **Correct PostgreSQL instance:** `postgresql://localhost:5452/iap`
  (dev stack database; host `localhost`, port `5452`, database `iap`).
  Connection string comes from `IAP_DATABASE_URI` in the layered env —
  **never print, echo, log, or paste the URI or any credential** (API keys,
  tokens, passwords) anywhere: not in terminals shared in reports, not in
  markdown, not in code, not in battery output. When a connection check is
  needed, parse and display **host/port/database only**.
- The dev `:5452` database is pg_guard-protected: run migrations only via
  the repo's documented flow, never ad-hoc DDL against it. Test procedure
  below is read-only plus battery runs through the API — no direct writes.

## 3. Test-case list (reuse, do not reinvent)

### 3.1 Unit gates (must stay green; run before and after any change)
- D3 shadow suite: `tests/unit/test_d3_shadow_runtime.py` +
  `tests/unit/test_d3_shadow_bundle.py` — **49 tests** (acceptance bar).
- `ruff check` + `ruff format --check` on touched files.
- Full unit suite: green except the **2 known pre-existing baseline-rev
  pin failures** (`test_proof_kernel_baseline.py` — pinned to an old rev;
  unrelated; do not "fix" by re-pinning without owner direction).

### 3.2 Live scratch-telemetry spot check (required when runtime capture behavior changes)
On a scratch tenant (never the frozen probe tenants), one L2 shadow run must show:
- `definition_shadow_trace` non-empty with `CONCLUDE_GATE` entries present,
- `shadow_definition_summary.capture_errors == 0`,
- T2/T3 mirror notes counted under `mirror_notes`/`mirror:T2*`, never as capture errors.
And one L2 flag-off control run must show:
- Native verdict parity with its shadow counterpart,
- **D3 shadow keys ABSENT** from loop telemetry (N21 artifact absence).

### 3.3 Convergence bar for declaring L2 fixed
- L2 PASS (ANSWERED, conclusion supported by cited evidence) in **repeated
  shadow runs** (minimum: the count that makes a second FAIL statistically
  untenable for the claim being made — state N and the reasoning),
- plus one fresh flag-off control with native parity and D3-key absence,
- plus gates in §3.1 green.

## 4. Runtime procedure (existing scripts only)

> Convention: `<TS>` = run tag (e.g. `20261024T215555`). Reports land in
> `reports/` (gitignored scratch — reference by path, never commit).

1. **Layer env** (§2, both files; verify with `env | grep -c IAP_` — count only, never values).
2. **Restart the worker with the shadow flag** and confirm single poller:
   ```bash
   pkill -f "investigation_agent_platform.main --worker"; sleep 2
   IAP_SHADOW_TYPED_DEFINITIONS=1 nohup uv run python -u -m investigation_agent_platform.main --worker > tmp/worker.log 2>&1 &
   echo $! > tmp/worker.pid
   ps aux | grep "main --worker" | grep -v grep   # expect wrapper + child only
   curl -sf http://localhost:8080/api/v1/health/ready
   ```
3. **Clean the scratch tenant scope** (never a frozen probe tenant):
   ```bash
   uv run python scripts/clean_battery.py --tenant dev-shadow-certify --app-id spring-sample-service --execute
   ```
4. **Shadow battery** (L2-focused; extend `--only` per the convergence bar):
   ```bash
   nohup uv run python scripts/live_battery.py --tenant dev-shadow-certify --only L2 \
     --timeout 600 --no-cleanup-prior --report reports/dev-shadow-L2-<TS>.md > tmp/L2-shadow-<TS>.log 2>&1 &
   ```
5. **Verify capture** (read-only DB/API checks): per-run trace non-empty,
   `CONCLUDE_GATE` present, `capture_errors == 0`, mirror notes counted
   (see §3.2). Any `INVALID_FLOAT`, pairing exception, or silent-drop
   signature fails the run immediately — stop and report.
6. **Control arm**: restart worker **without** the flag, re-run step 4,
   confirm native parity with the shadow counterpart and D3-key absence;
   a fresh native divergence stops the probe per the §6.2g precedent —
   record it, do not explain it away.
7. **Restore flagged worker** (shadow-ready) when done; re-clean scratch scope.
8. **Generate the report**: the battery markdown (`reports/dev-shadow-L2-<TS>.md`)
   plus a telemetry appendix (per-run: verdict, trace length, input_set list,
   summary counters, mirror counts). Compare verdict sequences across runs;
   state PASS/FAIL counts, any divergence, and whether the §3.3 bar is met.

## 5. Report acceptance

- The report states the mechanism (Problem 1/3) or the convergence claim
  (Problem 2/3) with falsifiers, not adjectives.
- Every number in the report traces to a retained row, a report file, or a
  gate output named by path. No tmp/ paths referenced.
- No code, migration, flag, or freeze change is implied by running this
  procedure; any fix it motivates returns to Problem 2/3 and the standing
  certification gate.
