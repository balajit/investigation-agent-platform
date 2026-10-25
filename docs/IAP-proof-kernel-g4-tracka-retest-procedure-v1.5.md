# Track A Retest Procedure v1.5 — D3-family fix verification

> Status: procedure, 2026-10-26. Supersedes v1.4. Runs on owner direction when a D3-family fix lands (mechanism named per the L2-D3 diagnosis; fix on its own branch). This procedure verifies; it does not implement, re-pin, merge, authorize G4 PASS, or authorize production behavior. Fingerprint, baseline, or freeze re-pinning without explicit owner direction is forbidden — a red pin is a report, never a self-service edit.

## 0. Revision note (v1.4 → v1.5)

Fifth read-only review found one narrow gap, agreed and fixed: Step 5's `grep ... || true` suppressed every grep failure, not just no-match. The filter now discriminates grep exit 1 (no matches — the only status that vacates the step) from exit 2+ (real error — stops the procedure). No other changes.

Fourth read-only review found two fail-fast gaps, both agreed and fixed: Step 0's clean-tree gate is now an enforced `exit 1` check (not a described expectation), and the bash block opens with `set -eu -o pipefail` so the stop rule is executed by the shell — a red command halts the block. (Historical note: this paragraph describes v1.4's then-current implementation, in which the grep no-match case was tolerated via `|| true`; v1.5 above replaces that with exit-1-vs-2+ discrimination.) The verified-empty Step 5 vacate flows through the explicit `if`, never through a swallowed failure.

Third read-only review found three defects plus one stale cross-reference, all agreed and fixed: Step 0's `<main>`/`<merge-base-SHA>` placeholders are runnable shell variables (`TRACKA_BASE_REF`, `MERGE_BASE`) — no angle brackets remain in any bash block; Step 5 splits the inventory pipeline so a `git diff` failure stops the procedure while only a verified-empty grep result vacates it; (G5 companion) P2 is stated in the assertion-bundle scope and G5 §11 names v1.3 as the execution candidate.

Second read-only review confirmed the structure and found five repairable defects, all agreed and fixed: the 49-test bar's absent-file citation is replaced by a durable branch/commit reference plus per-run collection (the count never rests on the absent file); the lint-fix retry now commits on the fix branch and restarts at Step 0 (clean-tree gate kept); Step 5 has an explicit empty-inventory guard; the baseline "unchanged" rule carries the exact failure signatures and their source lines; the env snapshot records variable names (values redacted except the controlled flag).

Read-only review of v1.0 found two blockers and seven gaps. All agreed and fixed: step order and stop rule are now single and coherent (§1–§2); the two baseline pins are named with an unchanged-verification rule (§3, signatures added in v1.2 §0); `<fix-base>`/`<fix-branch>` placeholders are replaced by a clean-tree + merge-base + frozen-inventory protocol (§1, Step 0); the 49-test bar is pinned to its two files and counted (§2); the convergence bar is pre-registered (N=10, independence, pairing key, failure threshold) (§5); the flag-off control is paired by an explicit key (§5); the runtime-trigger rides changed surfaces, not author categories (§4); the lint file list derives from the frozen inventory (§1, Step 5); scratch-report handling distinguishes the transient file from the durable transcription (§6); re-disposition scope is defined per fix family with residuals (§7).

## 1. Steps in order (each step's number is used unchanged in §2 and §3)

```bash
# Fail-fast is ENFORCED by the shell, not described: any red command below
# halts the block (the §1 stop rule, executed). The only branches around
# nonzero statuses are the explicit ones (merge-base/inventory captures,
# grep 1-vs-2+ discrimination); every expected-empty case flows through an
# explicit branch, never through a swallowed failure.
set -eu -o pipefail

# Step 0 — freeze the change inventory (read-only; nothing runs on a dirty tree).
# Every value below is a shell variable — no placeholders; paste the block as-is.
# The operator overrides the base ONLY by exporting TRACKA_BASE_REF beforehand
# (default origin/main, owner direction required for anything else).
TRACKA_BASE_REF="${TRACKA_BASE_REF:-origin/main}"
DIRTY=$(git status --porcelain)
# expect: EMPTY. Enforced: any output exits nonzero here — rev-parse,
# merge-base, and all later steps never run on a dirty tree.
[ -z "$DIRTY" ] || { echo "dirty tree — stop"; exit 1; }
git rev-parse HEAD
# record: fix-branch HEAD SHA.
MERGE_BASE=$(git merge-base HEAD "$TRACKA_BASE_REF") || { echo "merge-base failed — stop"; exit 1; }
# record: the merge-base SHA and the base ref actually used. For a fix branch
#   with merge commits, the merge-base rule still applies: the inventory is
#   merge-base...HEAD, never a guessed range.
git diff --name-only "$MERGE_BASE"...HEAD
# record verbatim: the FROZEN INVENTORY. Every later step draws file lists
#   from this output only. Regenerating it mid-run voids the run.
#   (Step 5 re-derives its lint list from the recorded MERGE_BASE value —
#   no scratch file, still frozen.)

# Step 1 — D3 acceptance pair (the 49-test bar, counted not assumed).
# The bar's origin is the L2-D3-fix branch, commit 0b7df3c, file
# docs/IAP-L2-D3-fix-test-procedure.md §3.1 (absent from the main tree —
# viewable via `git show 0b7df3c:docs/IAP-L2-D3-fix-test-procedure.md`).
# The bar does NOT rest on that file: the count is re-collected every run.
uv run python -m pytest -q -p no:cacheprovider tests/unit/test_d3_shadow_runtime.py tests/unit/test_d3_shadow_bundle.py
uv run python -m pytest -q -p no:cacheprovider --collect-only tests/unit/test_d3_shadow_runtime.py tests/unit/test_d3_shadow_bundle.py | tail -1
# record: collected count (expect 49) AND the run tally. A count deviation
# (new/removed tests in the fix) is reported with the inventory diff, never
# silently absorbed.

# Step 2 — D3 family floor + inventory-derived additions (mandatory floor, not blast-radius-optional)
uv run python -m pytest -q -p no:cacheprovider tests/unit/test_d3_definition_authorize.py tests/unit/test_d3_definition_observation.py tests/unit/test_d3_shadow_bundle.py tests/unit/test_d3_shadow_runtime.py tests/unit/test_d3_tap_isolation.py tests/unit/test_shadow_action_policy.py tests/unit/test_shadow_capture.py tests/unit/test_shadow_telemetry.py
# The eight files above run EVERY time (floor). ADDITIONALLY: every test file
# under tests/ in the frozen inventory runs too (append its paths to this
# command). Files outside tests/ draw no test additions, but their change is
# still covered by Steps 3-4. Record both tallies separately (floor vs additions).

# Step 3 — authoritative 14-file gate (fingerprints + kernel incl. C4)
uv run python scripts/proof_kernel_validator_fingerprints.py --check && uv run python -m pytest -q -p no:cacheprovider tests/unit/test_canon_identity.py tests/unit/test_manifest_evaluation.py tests/unit/test_typed_rules.py tests/unit/test_certificate_checker.py tests/unit/test_composition_invalidation.py tests/unit/test_publication_decision.py tests/unit/test_g5_composition_gate.py tests/unit/test_proof_kernel_fixture_integrity.py tests/unit/test_proof_kernel_validator_fingerprints.py tests/unit/test_proof_kernel_g4_component1_evidence.py tests/unit/test_proof_kernel_g4_component2_rules.py tests/unit/test_proof_kernel_g4_component3_properties.py tests/unit/test_proof_kernel_g4_component4_integration.py tests/unit/test_proof_kernel_premises.py

# Step 4 — full unit suite (regression net; §3 for the only accepted reds)
uv run python -m pytest -q -p no:cacheprovider tests/unit

# Step 5 — ruff on the frozen inventory's Python (re-derived from the recorded
# MERGE_BASE value, so the list is still frozen; last because lint cannot change
# behavior, but the REPORT must show final green). Statuses are discriminated,
# never swallowed: git-diff failure stops (above); grep exit 1 (no matches) is
# the ONLY status that vacates; grep exit 2+ (real error) stops. The `if`
# condition context suspends `set -e` for the capture, and `$?` is read as the
# first statement of the else branch — before any other command can clobber it.
INV_PY=$(git diff --name-only "$MERGE_BASE"...HEAD -- '*.py') || { echo "inventory pipeline failed — stop"; exit 1; }
TOUCHED_PY=""
if TOUCHED_PY=$(printf '%s\n' "$INV_PY" | grep -E "^(src|tests)/"); then
  uv run ruff check $TOUCHED_PY && uv run ruff format --check $TOUCHED_PY
else
  GREP_STATUS=$?
  if [ "$GREP_STATUS" -eq 1 ]; then
    echo "no Python in inventory — Step 5 vacated (recorded, not skipped)"
  else
    echo "inventory filter failed (grep status $GREP_STATUS) — stop"; exit 1
  fi
fi
# ($TOUCHED_PY is intentionally unquoted: one path per word. A red Step 5 is
# fixed by a NEW COMMIT on the fix branch, then the procedure restarts at
# Step 0 with the new HEAD SHA and a re-frozen inventory — the Step 0
# clean-tree gate is never bypassed, and lint edits never ride uncommitted.)
```

Stop rule (enforced, not described): the block opens with `set -eu -o pipefail`, so Steps 0–5 run top-to-bottom and a red command halts the block immediately — no proceeding to slower suites on a red fast suite, no lint after a red gate. The shell does the stopping; the operator reads the failure, fixes on the fix branch, and restarts (Step 0 for a lint fix with a new commit; Step 0 for anything else). No step is renumbered, reordered, or parallelized without owner direction.

## 2. Trigger and scope

Trigger: owner states a D3-family fix is ready for verification. Scope is the fix's blast radius PLUS the standing floor — never less than §1 in full:

1. Step 0 inventory (clean tree, recorded SHAs, frozen file list).
2. Step 1 acceptance pair (`test_d3_shadow_runtime.py` + `test_d3_shadow_bundle.py` — the 49-test bar; count verified every run).
3. Step 2 family floor (all eight D3/shadow files) + every test file in the frozen inventory.
4. Step 3 authoritative 14-file gate (includes the C4 20-test module).
5. Step 4 full unit suite.
6. Step 5 ruff on the frozen Python inventory.
7. Conditionally: §4 live scratch-telemetry spot check (when §4's trigger fires) and §5 convergence bar (only when the fix claims the L2 flip is gone).

## 3. Pass criteria

- **Step 0:** clean tree, SHAs recorded, inventory frozen. Anything else is a stop, not a waive.
- **Step 1:** the exact bar from the L2-D3-fix branch (`0b7df3c:docs/IAP-L2-D3-fix-test-procedure.md` §3.1) — `test_d3_shadow_runtime.py` + `test_d3_shadow_bundle.py` collect 49 tests and all pass. Record collected count AND tally; a count change (new/removed tests in the fix) is reported with the inventory diff, never silently absorbed.
- **Step 2:** floor (eight files) all green + additions all green; tallies recorded separately.
- **Step 3:** fingerprints clean (6 modules) + gate all-pass. Baseline at decision time: `290 passed, 1 skipped` (pre-existing G5 live-DB skip). Counts may grow if the fix adds tests — transcribe actuals, never edit expectations to match.
- **Step 4:** green EXCEPT exactly these two named failures, verified unchanged:
  - `tests/unit/test_proof_kernel_baseline.py::test_baseline_check_passes`
  - `tests/unit/test_proof_kernel_baseline.py::test_baseline_is_frozen_and_reproducible`
  
  "Unchanged" means: the same two node IDs fail, with the same assertion signatures recorded here (observed 2026-10-26; `HEAD` value below is an example — verify against `git rev-parse HEAD` at run time, do not copy it):
  - `test_baseline_check_passes` (`test_proof_kernel_baseline.py:26-27`): `assert 1 == 0` where `1 = baseline.main(check_only=True)` — the check entrypoint itself exits nonzero on the pinned-manifest mismatch.
  - `test_baseline_is_frozen_and_reproducible` (`test_proof_kernel_baseline.py:30-35`): `AssertionError` on `assert manifest == fresh_manifest` with the differing item `{'git_rev': '1389471dd566039e5433fab3df2fd226d0ca4c8a'} != {'git_rev': '<current-HEAD-full-SHA>'}` — pinned rev vs HEAD, nothing else differs (run with `-vv` if the short diff omits items).
  
  Verify by focused rerun of the file and diffing the failure lines against these signatures. Any third failure, any different assertion in these two tests (different line, different diff shape, additional differing items), or any other red is a new finding: stop, report, do not re-pin, do not "fix" the expectation.
- **Environment snapshot (Step 4 context, recorded not assumed):** variable NAMES sorted (values redacted — a count alone cannot diagnose an env-driven failure):
  ```bash
  env | grep -oE '^IAP_[A-Z_0-9]+' | sort
  ```
  plus the VALUE of `IAP_LIVE_TESTS` only (must be unset/empty/0 for unit runs — a live gate leaking into the unit run voids it). Required controlled set: `IAP_LIVE_TESTS` unset/empty/0; every other `IAP_*` name recorded, values never printed. The uncontrolled rerun's three environment-driven failures are the precedent: if environment-driven failures appear, they are reported as additional failures with this snapshot attached, never presumed clean.
- **Step 5:** ruff clean on the frozen list (or "Step 5 vacated" recorded). Red → commit the fix on the fix branch, restart at Step 0.

## 4. Live scratch-telemetry spot check (conditional)

Trigger (widened — not the author's category): the frozen inventory touches ANY of: runtime capture code, shadow/telemetry/pairing code, loop-telemetry emission, OR the telemetry contract differs from frozen (compare emitted N21/shadow keys against the freeze record). When in doubt, run it — a skipped check needs a one-line trigger-negative justification citing the inventory.

Staging dev stack only (`host/port/database` display only — never URIs or credentials). Scratch tenant only; frozen probe tenants untouched. One L2 shadow run must show: `definition_shadow_trace` non-empty with `CONCLUDE_GATE` entries, `shadow_definition_summary.capture_errors == 0`, T2/T3 mirror notes under `mirror_notes`/`mirror:T2*` (never as capture errors). One L2 flag-off control run, paired per §5's key to its shadow counterpart, must show native verdict parity and D3 shadow keys ABSENT from loop telemetry. Any `INVALID_FLOAT`, pairing exception, or silent-drop signature fails the run immediately — stop and report. Reports land in `reports/` (gitignored scratch); §6 governs what survives them.

## 5. L2 convergence bar (only when the fix claims the L2 flip is gone)

Pre-register BEFORE collecting (write it in the report header first; changing N mid-collection voids the run):

- **N = 10** consecutive L2 shadow runs, all PASS (ANSWERED, conclusion supported by cited evidence), zero FAIL, zero divergence. Stop on the first FAIL or the first fresh native divergence (per the §6.2g precedent — record it, do not explain it away).
- **Independence:** separate worker processes/runs; same L2 case identity + same code rev (`HEAD` SHA recorded); `-p no:cacheprovider`; fresh scratch scope per run (`clean_battery.py` per the L2-D3 procedure §4 step 3 — same-case replay never rides cached state).
- **Pairing key** (shadow run ↔ flag-off control): `(L2 case id, code rev HEAD, tenant, battery flags/timeout, worker flag state)`. The control pairs to the FINAL shadow run: same key, flag off. Parity + D3-key absence required.
- **Failure threshold:** zero. One FAIL whose trace shows shadow-path causation (tap/observer/pairing errors, shadow-induced native path change, flag-dependent difference with all else equal) reopens the interference question and voids the convergence claim. One FAIL with the known struggle-run signature is recorded as variance data and ALSO voids the claim — "statistically untenable" is not argued post hoc; the bar is ten clean or nothing.
- Plus: one fresh flag-off control (above) and §1 Steps 1–5 green on the fix branch.

## 6. Report format (durable record vs scratch)

One durable record containing: fix branch + merge-base + HEAD SHAs, frozen inventory verbatim, mechanism named (link to the diagnosis), §1 outputs transcribed verbatim (commands, tallies, collected counts, any red with failure attribution), env snapshot (§3), §4/§5 outputs if run (PASS/FAIL counts per run, divergence notes), and what changed versus the G4 disposition baseline (`290 passed, 1 skipped` gate; `3576 passed, 4 skipped, 6 xfailed` + 2 frozen pins full suite; C4 20/20). Every number traces to a gate output or a file named by path.

Scratch rule: `reports/` files are transient evidence, referenced by path but never relied upon as the record. The transcription above IS the durable record (disposition package appendix + ledger); after scratch cleanup, nothing of value may live only in a deleted path. No `tmp/` paths are referenced in durable artifacts (repo rule).

## 7. Re-disposition path and scope per fix family

The report goes to the owner with one of two recommendations: (a) evidence supports G4 re-disposition (state PASS or continued PARTIAL with what remains), or (b) evidence does not (state the gap). The owner decides; this procedure creates no disposition by itself. A green procedure result alone authorizes nothing — no G4 PASS, no production behavior.

Scope rule: the report must name which G4 criteria the fixed family touches (C1 evidence / C2 rules / C3 properties / C4 integration / gate), derived from the fix's mechanism section — the mapping rides the mechanism, not a fixed table, because a family label alone does not determine blast radius. Residuals ride along explicitly: anything the fix does not cover (other D3 families, G5 atomicity, G7/G8) is listed as still-open with its owning procedure, never implied closed.

On a PASS decision: update the disposition package, traceability, and ledger — the same record pattern as the 2026-10-26 PARTIAL.
