#!/usr/bin/env bash
# run_battery.sh — cleanup first, then tests. Never tests against stale rows.
#
#   1. Layer repo env (./.env.local, then ~/bin/.iap.env.local).
#   2. Delete prior battery-scope rows (fail-closed: abort if cleanup fails).
#   3. Run the unit suite (default) or the live battery (--live).
#
# Usage:
#   scripts/run_battery.sh [--tenant ID] [--app-id ID] [--live]
#       [--force-cleanup] [--only L1,L2] [--timeout SECS] [-- <pytest args>]
#
# Examples:
#   scripts/run_battery.sh
#   scripts/run_battery.sh --live
#   scripts/run_battery.sh --tenant n-batt --live --only L1,L2
#   scripts/run_battery.sh -- -k "battery or phase4" -q
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TENANT="n-batt"
APP_ID="spring-sample-service"
LIVE=false
FORCE_CLEANUP=""
ONLY=""
TIMEOUT="600"
PYTEST_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tenant) TENANT="$2"; shift 2 ;;
        --tenant=*) TENANT="${1#*=}"; shift ;;
        --app-id) APP_ID="$2"; shift 2 ;;
        --app-id=*) APP_ID="${1#*=}"; shift ;;
        --live) LIVE=true; shift ;;
        --force-cleanup) FORCE_CLEANUP="--force-cleanup"; shift ;;
        --only) ONLY="$2"; shift 2 ;;
        --only=*) ONLY="${1#*=}"; shift ;;
        --timeout) TIMEOUT="$2"; shift 2 ;;
        --timeout=*) TIMEOUT="${1#*=}"; shift ;;
        --) shift; PYTEST_ARGS=("$@"); break ;;
        -h|--help)
            sed -n '2,20p' "$0"
            exit 0
            ;;
        *) echo "unknown flag: $1" >&2; exit 2 ;;
    esac
done

# 1. Env layering (repo defaults, then operator overrides). Secrets are
# sourced, never printed.
set -a
[[ -f ./.env.local ]] && source ./.env.local
[[ -f ~/bin/.iap.env.local ]] && source ~/bin/.iap.env.local
set +a

# 2. Cleanup gate: prior battery rows must go before anything runs.
# Fail-closed — a cleanup failure aborts instead of testing stale rows.
echo "==> cleanup prior battery rows (tenant=$TENANT app=$APP_ID)" >&2
# shellcheck disable=SC2086
uv run python scripts/clean_battery.py \
    --tenant "$TENANT" --app-id "$APP_ID" $FORCE_CLEANUP --execute

# 3. Tests.
if [[ "$LIVE" == true ]]; then
    echo "==> live battery (tenant=$TENANT)" >&2
    # shellcheck disable=SC2086
    exec uv run python scripts/live_battery.py \
        --tenant "$TENANT" --app-id "$APP_ID" ${ONLY:+--only "$ONLY"} \
        --timeout "$TIMEOUT" --no-cleanup-prior
else
    echo "==> unit suite" >&2
    if [[ ${#PYTEST_ARGS[@]} -gt 0 ]]; then
        exec uv run python -m pytest "${PYTEST_ARGS[@]}"
    else
        exec uv run python -m pytest tests/unit -q
    fi
fi
