#!/usr/bin/env bash
# verify-platform.sh — run the live integration suite against a running stack.
#
# Usage:
#   ./scripts/verify-platform.sh              # uses .env.local + localhost:PORT
#   ./scripts/verify-platform.sh -k infra     # pass through extra pytest args
#   IAP_LIVE_BASE_URL=http://host:8000 ./scripts/verify-platform.sh
#
# Requires: stack up (./scripts/start-platform.sh). Exits non-zero on any failure.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f .env.local && -z "${IAP_DATABASE_URI:-}" ]]; then
  # shellcheck disable=SC1091
  set -a; source .env.local; set +a
fi

export IAP_LIVE_TESTS=1
export IAP_LIVE_BASE_URL="${IAP_LIVE_BASE_URL:-http://localhost:${PORT:-8000}}"

if ! curl -sf "${IAP_LIVE_BASE_URL}/api/v1/health/live" >/dev/null 2>&1; then
  echo "error: API not reachable at ${IAP_LIVE_BASE_URL} (start it first: ./scripts/start-platform.sh)" >&2
  exit 1
fi

uv run pytest tests/integration/test_live_platform.py -v "$@"
