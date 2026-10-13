#!/usr/bin/env bash
# run-investigation-journey.sh — drive one investigation to COMPLETED, verifying every stage.
#
# Usage:
#   ./scripts/run-investigation-journey.sh [--env-file PATH] [--api-base URL]
#     [--tenant ID] [--journey-timeout SECS] [--loop-timeout SECS]
#
# Requires: stack up (./scripts/start-platform.sh). All state changes go
# through HTTP service invocation; the driver only READS Postgres/Elastic
# for persistence assertions. Batch intake is never called.
set -euo pipefail

ENV_FILE=""
ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file=*) ENV_FILE="${1#--env-file=}" ;;
    --env-file) ENV_FILE="${2:-}"; shift ;;
    --api-base=*|--tenant=*|--journey-timeout=*|--loop-timeout=*|--run-id=*|--app-id=*)
      ARGS+=("$1") ;;
    --api-base|--tenant|--journey-timeout|--loop-timeout|--run-id|--app-id)
      ARGS+=("$1" "${2:-}"); shift ;;
    --help|-h)
      echo "Usage: $0 [--env-file PATH] [--api-base URL] [--tenant ID] [--journey-timeout SECS] [--loop-timeout SECS] [--run-id ID] [--app-id ID]"
      exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

if [[ -z "$ENV_FILE" ]]; then
  if [[ -f .env.local ]]; then ENV_FILE=.env.local
  elif [[ -f "$HOME/bin/.iap.env.local" ]]; then ENV_FILE="$HOME/bin/.iap.env.local"
  fi
fi
if [[ -n "$ENV_FILE" && -f "$ENV_FILE" ]]; then
  echo "Loading env from $ENV_FILE"
  # shellcheck disable=SC1090
  set -a; source "$ENV_FILE"; set +a
fi

# The second credentials file carries the real LLM keys when present.
if [[ -f "$HOME/bin/.iap.env.local" && "$ENV_FILE" != "$HOME/bin/.iap.env.local" ]]; then
  echo "Loading keys from $HOME/bin/.iap.env.local"
  # shellcheck disable=SC1090
  set -a; source "$HOME/bin/.iap.env.local"; set +a
fi

API_BASE="${IAP_API_BASE:-http://127.0.0.1:8000}"
if ! curl -sf --max-time 10 "${API_BASE}/api/v1/health/live" >/dev/null 2>&1; then
  echo "error: API not reachable at ${API_BASE} (start it first: ./scripts/start-platform.sh)" >&2
  exit 1
fi

exec uv run python -u scripts/investigation_journey.py --api-base "$API_BASE" "${ARGS[@]}"
