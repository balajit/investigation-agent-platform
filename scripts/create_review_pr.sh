#!/bin/bash
# scripts/create_review_pr.sh — open the retrospective CodeRabbit review PR.
#
# The REV_BATTERY work already landed on origin/main, so there is no feature
# branch to review in the normal flow. The review branch
# (review/retrospective-full-coderabbit) replays that exact tree for review.
# This script opens the PR with the @coderabbitai trigger in the body.
#
# Usage:
#   ./scripts/create_review_pr.sh [--dry-run]
#
# Requires: gh authenticated as an identity allowed to open PRs in the repo.
# If API creation is denied (e.g. EMU token restriction), the script prints
# the pre-filled compare URL as a fallback for one-click creation in a browser.
#
# macOS bash 3.2 compatible: no associative arrays, no mapfile.

set -u

BASE="main"
HEAD_BRANCH="review/retrospective-full-coderabbit"
TITLE="Retrospective review: battery verification hardening + python-lang analyzer (do not merge)"
DRY_RUN=0

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        -h|--help)
            echo "Usage: $0 [--dry-run]"
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            echo "Usage: $0 [--dry-run]" >&2
            exit 2
            ;;
    esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

fail() { echo "create_review_pr: $1" >&2; exit "${2:-1}"; }

command -v gh >/dev/null 2>&1 || fail "gh CLI not found in PATH"
if [ "$DRY_RUN" = "1" ]; then
    echo "DRY RUN — would execute (from repo root $ROOT):"
    echo "  gh pr create --base $BASE --head $HEAD_BRANCH --title \"$TITLE\" --body-file <rendered-body>"
    echo "Body preview (first 3 lines of the embedded body):"
    echo "@coderabbitai full review"
    echo ""
    echo "> **Retrospective review vehicle — DO NOT MERGE.** Content already landed on"
    exit 0
fi
gh auth status >/dev/null 2>&1 || fail "gh is not authenticated (run gh auth login)"

REMOTE_URL="$(git ls-remote --heads origin "$HEAD_BRANCH" 2>/dev/null | awk '{print $1}')"
[ -n "$REMOTE_URL" ] || fail "branch '$HEAD_BRANCH' not found on origin (push it first)"

EXISTING="$(gh pr list --head "$HEAD_BRANCH" --state open --json url --jq '.[0].url' 2>/dev/null)"
if [ -n "$EXISTING" ] && [ "$EXISTING" != "null" ]; then
    echo "An open PR already exists for $HEAD_BRANCH:"
    echo "$EXISTING"
    exit 0
fi

BODY_FILE="$(mktemp /tmp/review_pr_body.XXXXXX)"
trap 'rm -f "$BODY_FILE"' EXIT INT TERM
cat > "$BODY_FILE" <<'PRBODY_EOF'
@coderabbitai full review

> **Retrospective review vehicle — DO NOT MERGE.** Content already landed on
> `origin/main` as `527f369`. This branch replays that exact tree (verified
> byte-identical) so the complete change set gets a full review. Follow-ups
> from review will land as separate PRs.

## Summary

Two workstreams, one commit: live-battery-driven verification hardening
(`REV_BATTERY` close-out, 5/10 → 9/9+1 PARTIAL across four battery runs) plus
the Python-lang deterministic fact analyzer (mirrors the Java pipeline).

## What changed

**Verification engine (`application/investigation/verification.py`, +345)**

- Caller-enumeration guard compares file stems against *all* counterpart
  segments (class + method), not just the last one — L2#1 failed on
  `OrderServiceRestProxy.submit` vs file stem `OrderServiceRestProxy`.
- `statement_range_reads` bare-stem fallback: caller-enumeration claims name
  bare classes, so slash-path matching saw nothing and banked call-site ranges
  never joined the bound set (L2#2 shape).
- Per-assessment same-claim sibling repair: a cited search-hit/comment row is
  completed by same-file bound range rows instead of failing on the pointer.
- `absence_outcome` four-state classification
  (`NO_SCOPE` / `EMPTY_SEARCH_PROVED` / `NO_SEARCH` / `SCOPE_MISMATCH`);
  `absence_proof` delegates to it.
- `next_check_for_abstention` maps kind-gate phrases (caller enumeration,
  no-cited-call-site, behavior-runtime, Java resolution, empty-bound with
  cross-investigation key) to targeted re-verify directives.
- `_call_shown(..., exclude_construction=...)` + `_instance_call_rows`:
  `new Callee()` construction lines no longer prove a "who calls X" claim;
  bare-class callees verify through a same-file declaration + invocation join.
- Resolution-aware conclusion mapping: bound RESOLVED code-fact identities
  join the mapped corpus; sentences restating a bound fact (subject + literal)
  no longer demote to `likely` via `CONCLUSION_UNMAPPED`.

**History provider (`infrastructure/evidence/code/git.py`, +62)**

- Bare-basename resolution against the tip tree (unique match wins; zero or
  ambiguous raise `CodePathNotFoundError` unwrapped so the gateway degrades
  as a miss and never banks a false empty-search proof). Live L3 banked zero
  rows for `OrderServiceImpl.java` while the file had changed in all 3 commits.
- `CodePathNotFoundError` re-raised unwrapped past the blanket
  `ExecutionError` handler.

**Java guarded-return resolver (`analysis/java/method_return_facts.py`)**

- Tolerant reparse stripping trailing brace-only lines (GET_CODE windows
  anchored on a method often include the enclosing class's closing brace,
  which breaks the method-only tree-sitter parse — live L5 6-19 window).
- Declaring-type basename fallback when the window carries no enclosing class,
  with the resolved subject qualified by the requested declaring type.

**Worker (`application/worker/activities.py`)**

- Conclusion-gate denial abstention leads with the verification explanation so
  the persisted `next_check` names the failed proof instead of the generic
  chain message.

**Harnesses (`scripts/`)**

- `live_battery.py`: per-FAIL `bucket:` taxonomy line (retrieval /
  proof-construction / policy / genuine-insufficiency).
- `tranche_b_battery.py::_check_values` uses row identity (`_row_texts`,
  path included) like `_check_callers`, instead of snippet-only matching.

**Tests** (all green: full unit suite 2510 passed / 3 skipped, ruff clean,
mypy region-clean)

- `tests/unit/test_rev_battery_goldens.py`, `tests/unit/test_git_history_basename.py`,
  `tests/unit/test_java_method_returns.py` (+3 live-shape cases),
  `tests/integration/test_verify_conclude_path.py` (new, hermetic end-to-end
  bound→verify→gate→next_check suite, incl. L2/L3/L5 live shapes),
  `tests/unit/test_battery_scoring.py` (+3 row-identity cases).
- Note: `tests/integration/test_verify_conclude_path.py` is intentionally
  hermetic (no live gate) — it pins the composed worker path, not the stack.

**Docs/ledger**

- `docs/REV_BATTERY_CLOSE_PLAN.md` (new, phased close-out plan + 4 build-progress notes),
  `docs/IAP_Arch_Eng_Recommendations.log` (+13 `PLANNED` entries).

## What reviewers should focus on

1. `call_site_proof` bare-callee semantics (`verification.py`): the
   construction-vs-invocation split and the same-file join — especially
   whether the declaration regex can over-match (e.g. `Map<String, X> var`,
   annotations, multi-variable declarations) and admit a false positive.
2. `validate_location_claim` + kind-gate interplay: CALL_EDGE answers can still
   publish via the location fast path — confirm the new invocation requirement
   actually gates every path to `ANSWERED`, not just `check_claim_kind`.
3. `_resolve_bare_basename` failure modes: nested-repo path handling (tree_path
   is repo-relative, search runs on the resolved repo — confirm multi-root
   correctness) and the 20k-entry scan bound on large trees.
4. `sentence_mapped` / `_resolution_grounded`: value+subject matching is
   substring-based — look for fabrication vectors where a resolved literal
   (e.g. `"STANDARD"`) appears incidentally in an unrelated sentence.
5. Harness `_check_values` change: this moves the measuring stick (path counts
   as evidence). If you disagree, L5 stays `PARTIAL` as the honest answer —
   say so and it reverts to a one-liner.
6. The intentionally deferred items: absence-tier policy (absence stays
   `INSUFFICIENT_EVIDENCE`), workflow-level generic `next_check` threading,
   per-kind scoring presets — all recorded in the plan/ledger, not in this diff.

## Testing notes

- `uv run pytest tests/unit -q` → 2510 passed, 3 skipped (33s).
- `uv run ruff check` clean on all touched files; `mypy` clean in all edited
  regions (repo has pre-existing baseline errors elsewhere).
- Live batteries on the worker (separate runs, `reports/live_battery_*.md`):
  `030700` 5/5 → `035908` 5/5 → `041339` 7/3 → `042953` 8/2 PARTIAL →
  `115939` 9/1 PARTIAL. Remaining L2#2 shape is retrieval + formulation work
  (bank real call-site ranges, method-qualified callee), enforced honest by
  the new verifier semantics.
PRBODY_EOF

if PR_URL="$(gh pr create --base "$BASE" --head "$HEAD_BRANCH" --title "$TITLE" --body-file "$BODY_FILE" 2>/tmp/review_pr_err.txt)"; then
    echo "PR created:"
    echo "$PR_URL"
else
    echo "gh pr create failed (this account may lack PR-create permission):" >&2
    cat /tmp/review_pr_err.txt >&2
    rm -f /tmp/review_pr_err.txt
    echo "" >&2
    echo "Fallback: open this URL in a browser (title/body pre-filled):" >&2
    echo "https://github.com/balajit/investigation-agent-platform/compare/${BASE}...${HEAD_BRANCH}?expand=1" >&2
    exit 1
fi
rm -f /tmp/review_pr_err.txt
