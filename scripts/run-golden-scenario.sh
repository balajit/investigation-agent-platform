#!/usr/bin/env bash
# run-golden-scenario.sh — verify the investigation path end to end.
#
# Stages:
#   0. Config gate (fail fast on placeholder keys / unregistered model)
#   1. Infra pre-checks (postgres, elastic, temporal, neo4j, kafka, API)
#   2. Deep checks via scripts/golden_scenario.py (LLM, Elastic+mappings, API golden path)
#
# Usage:
#   ./scripts/run-golden-scenario.sh [--env-file PATH] [--api-base URL]
#     [--tenant ID] [--skip-llm] [--skip-elastic] [--start] [--timeout SECS]
#
# Env: IAP_DATABASE_URI, IAP_LLM_API_KEY (or source your env file first).
#   Default env file: .env.local, fallback ~/bin/.iap.env.local.
set -euo pipefail

ENV_FILE=""
API_BASE="${IAP_API_BASE:-http://localhost:8000}"
TENANT="${IAP_GOLDEN_TENANT:-tenant-a}"
APP_ID="${IAP_GOLDEN_APP_ID:-example-app}"
TIMEOUT="${TIMEOUT:-10}"
SKIP_LLM=false
SKIP_ELASTIC=false
DO_START=false
PASS_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file=*) ENV_FILE="${1#--env-file=}" ;;
    --env-file) ENV_FILE="${2:-}"; shift ;;
    --api-base=*) API_BASE="${1#--api-base=}" ;;
    --api-base) API_BASE="${2:-}"; shift ;;
    --tenant=*) TENANT="${1#--tenant=}" ;;
    --tenant) TENANT="${2:-}"; shift ;;
    --app-id=*) APP_ID="${1#--app-id=}" ;;
    --app-id) APP_ID="${2:-}"; shift ;;
    --timeout=*) TIMEOUT="${1#--timeout=}" ;;
    --timeout) TIMEOUT="${2:-$TIMEOUT}"; shift ;;
    --skip-llm) SKIP_LLM=true; PASS_ARGS+=(--skip-llm) ;;
    --skip-elastic) SKIP_ELASTIC=true; PASS_ARGS+=(--skip-elastic) ;;
    --start) DO_START=true; PASS_ARGS+=(--start) ;;
    --help|-h)
      echo "Usage: $0 [--env-file PATH] [--api-base URL] [--tenant ID] [--app-id ID] [--skip-llm] [--skip-elastic] [--start] [--timeout SECS]"
      exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

# -- env file -------------------------------------------------------------
if [[ -z "$ENV_FILE" ]]; then
  if [[ -f .env.local ]]; then ENV_FILE=.env.local
  elif [[ -f "$HOME/bin/.iap.env.local" ]]; then ENV_FILE="$HOME/bin/.iap.env.local"
  fi
fi
if [[ -n "$ENV_FILE" && -f "$ENV_FILE" ]]; then
  echo "Loading env from $ENV_FILE"
  # shellcheck disable=SC1090
  set -a; source "$ENV_FILE"; set +a
elif [[ -n "$ENV_FILE" ]]; then
  echo "error: env file not found: $ENV_FILE" >&2; exit 2
fi

PASS_ARGS+=(--api-base "$API_BASE" --tenant "$TENANT" --app-id "$APP_ID")
export IAP_API_BASE="$API_BASE" IAP_GOLDEN_TENANT="$TENANT" IAP_GOLDEN_APP_ID="$APP_ID"

pass=0; fail=0; results=()
ok()   { echo "  PASS  $1"; pass=$((pass+1)); results+=("PASS $1"); }
bad()  { echo "  FAIL  $1 -- $2"; fail=$((fail+1)); results+=("FAIL $1 :: $2"); }
skip() { echo "  SKIP  $1 -- $2"; results+=("SKIP $1 :: $2"); }

echo "== Stage 0: config gate =="
# Placeholder API key blocks everything downstream (unless --skip-llm).
if [[ "$SKIP_LLM" == true ]]; then
  skip "llm-api-key" "--skip-llm"
elif [[ -z "${IAP_LLM_API_KEY:-}" ]] || [[ "${IAP_LLM_API_KEY}" == *"your-actual-key-here"* ]]; then
  bad "llm-api-key" "IAP_LLM_API_KEY missing or placeholder; export the real key"
else
  ok "llm-api-key present"
fi
# Model must be in MODEL_REGISTRY (single source of truth: factory.py).
# Query the registry live so newly approved models pass without editing this script.
MODEL="${IAP_LLM_MODEL:-gpt-4o}"
REGISTRY_MODELS="$(uv run python -c "from investigation_agent_platform.infrastructure.reasoning.factory import MODEL_REGISTRY; print(' '.join(sorted(MODEL_REGISTRY)))" 2>/dev/null || echo "gpt-4o gpt-4o-mini gpt-4-turbo gpt-3.5-turbo claude-3-5-sonnet claude-3-opus claude-3-haiku claude-sonnet-4")"
if [[ " $REGISTRY_MODELS " == *" $MODEL "* ]]; then
  ok "llm-model $MODEL (registry)"
else
  if [[ "$SKIP_LLM" == true ]]; then
    skip "llm-model" "'$MODEL' not in registry, skipped via --skip-llm"
  else
    bad "llm-model" "'$MODEL' not in MODEL_REGISTRY ($REGISTRY_MODELS); for Azure set IAP_LLM_MODEL to a base id and IAP_AZURE_OPENAI_DEPLOYMENT='$IAP_AZURE_OPENAI_DEPLOYMENT' to the deployment name"
  fi
fi
if [[ "${IAP_LLM_PROVIDER:-openai}" == "azure" ]]; then
  [[ -n "${IAP_AZURE_OPENAI_ENDPOINT:-}" ]] && ok "azure-endpoint" || bad "azure-endpoint" "IAP_AZURE_OPENAI_ENDPOINT required for provider=azure"
  [[ -n "${IAP_AZURE_OPENAI_DEPLOYMENT:-}" ]] && ok "azure-deployment" || bad "azure-deployment" "IAP_AZURE_OPENAI_DEPLOYMENT required for provider=azure"
fi
if [[ -z "${IAP_DATABASE_URI:-}" ]]; then
  bad "database-uri" "IAP_DATABASE_URI must be set"
else
  ok "database-uri present"
fi

echo "== Stage 1: infra pre-checks (timeout ${TIMEOUT}s) =="
# Postgres
if command -v pg_isready >/dev/null 2>&1; then
  if pg_isready -h localhost -p 5452 -U iap >/dev/null 2>&1 || pg_isready -h localhost -p 5432 -U iap >/dev/null 2>&1; then ok "postgres reachable"
  else bad "postgres" "pg_isready failed on 5452/5432"; fi
else skip "postgres" "pg_isready not installed; deep check via API readiness"
fi
# Elasticsearch
if curl -sf --max-time "$TIMEOUT" http://localhost:9200 >/dev/null 2>&1; then ok "elasticsearch :9200"
else bad "elasticsearch" "curl http://localhost:9200 failed"; fi
# Temporal gRPC (port open) + UI
if (echo > /dev/tcp/localhost/7233) >/dev/null 2>&1; then ok "temporal gRPC :7233 open"
else bad "temporal" "TCP localhost:7233 refused"; fi
if curl -sf --max-time "$TIMEOUT" http://localhost:8233 >/dev/null 2>&1; then ok "temporal-ui :8233"
else bad "temporal-ui" "curl http://localhost:8233 failed"; fi
# Neo4j (warn-only: topology is optional unless required)
if curl -sf --max-time "$TIMEOUT" http://localhost:7474 >/dev/null 2>&1; then ok "neo4j :7474"
else
  if [[ "${IAP_TOPOLOGY_REQUIRED:-false}" == "true" ]]; then bad "neo4j" "required but unreachable"
  else skip "neo4j" "not reachable (ok unless IAP_TOPOLOGY_REQUIRED=true)"; fi
fi
# Kafka (warn-only)
if (echo > /dev/tcp/localhost/9093) >/dev/null 2>&1; then ok "kafka :9093 open"
else skip "kafka" "TCP localhost:9093 refused (ok unless IAP_REQUIRE_BROKER=true)"; fi
# API live
if curl -sf --max-time "$TIMEOUT" "$API_BASE/api/v1/health/live" >/dev/null 2>&1; then ok "api live $API_BASE"
else bad "api-live" "curl $API_BASE/api/v1/health/live failed — is the API running? (scripts/start-platform.sh)"; fi

if [[ $fail -gt 0 ]]; then
  echo "--- pre-checks: $pass passed, $fail failed ---"
  echo "Fix the FAIL lines above, then rerun. Skipping deep checks."
  exit 1
fi

echo "== Stage 2: deep checks (LLM + Elastic + API golden path) =="
if [[ "$SKIP_LLM" == true ]]; then echo "(--skip-llm)"; fi
if [[ "$SKIP_ELASTIC" == true ]]; then echo "(--skip-elastic)"; fi
if [[ "$DO_START" == true ]]; then echo "(--start: will POST /start, needs Temporal + worker)"; fi
uv run python scripts/golden_scenario.py "${PASS_ARGS[@]}"
rc=$?
if [[ $rc -eq 0 ]]; then echo "GOLDEN SCENARIO: PASS"; else echo "GOLDEN SCENARIO: FAIL (see JSON above)"; fi
exit $rc
