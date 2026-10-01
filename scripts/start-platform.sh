#!/usr/bin/env bash
# start-platform.sh — bring up the Investigation Agent Platform locally.
#
# Order: compose services -> wait for readiness -> alembic migrate -> API + worker.
# Usage:
#   ./scripts/start-platform.sh            # full stack (API + worker)
#   ./scripts/start-platform.sh --no-worker
#   ./scripts/start-platform.sh --restart  # restart API/worker only (infra untouched)
#   ./scripts/start-platform.sh --stop     # stop API/worker + compose stop (volumes kept)
#
# Required env: IAP_DATABASE_URI, IAP_LLM_API_KEY
# Optional env: IAP_TOPOLOGY_* (see docs/IAP-implementation-part7-knowledge-v1.md D7),
#   PORT (default 8000), COMPOSE_FILE (default docker/docker-compose.yml).
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker/docker-compose.yml}"
PORT="${PORT:-8000}"
TIMEOUT="${TIMEOUT:-120}"
RUN_API=true
RUN_WORKER=true
RESTART_ONLY=false

# `uv run` wrappers exit on SIGTERM but orphan the real child process,
# which keeps holding the port. So pid-file kills alone are not enough:
# sweep the distinctive command lines too. Workers stuck in bridge I/O
# may ignore SIGTERM, hence the SIGKILL fallback.
stop_app() {
  [[ -f tmp/api.pid ]] && kill "$(cat tmp/api.pid)" 2>/dev/null || true
  [[ -f tmp/worker.pid ]] && kill "$(cat tmp/worker.pid)" 2>/dev/null || true
  pkill -f "uvicorn investigation_agent_platform.main:app" 2>/dev/null || true
  pkill -f "investigation_agent_platform.main --worker" 2>/dev/null || true
  sleep 2
  pkill -9 -f "uvicorn investigation_agent_platform.main:app" 2>/dev/null || true
  pkill -9 -f "investigation_agent_platform.main --worker" 2>/dev/null || true
  rm -f tmp/api.pid tmp/worker.pid
}

for arg in "$@"; do
  case "$arg" in
    --no-api) RUN_API=false ;;
    --no-worker) RUN_WORKER=false ;;
    --restart) RESTART_ONLY=true ;;
    --stop)
      echo "Stopping API/worker (if running) and compose services..."
      stop_app
      docker compose -f "$COMPOSE_FILE" stop
      exit 0
      ;;
    --timeout=*) TIMEOUT="${arg#--timeout=}" ;;
    --compose-file=*) COMPOSE_FILE="${arg#--compose-file=}" ;;
    --help|-h)
      echo "Usage: $0 [--no-api] [--no-worker] [--restart] [--stop] [--timeout=SECS] [--compose-file=PATH]"
      exit 0
      ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

if [[ -z "${IAP_DATABASE_URI:-}" || -z "${IAP_LLM_API_KEY:-}" ]]; then
  echo "error: IAP_DATABASE_URI and IAP_LLM_API_KEY must be set" >&2
  exit 2
fi
if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "error: compose file not found: $COMPOSE_FILE" >&2
  exit 2
fi
command -v docker >/dev/null || { echo "error: docker is required" >&2; exit 2; }

mkdir -p tmp

# --restart: bounce the application processes only. Infrastructure
# (compose services) and migrations are left untouched.
if [[ "$RESTART_ONLY" == true ]]; then
  echo "Restarting application (API/worker)..."
  stop_app
  sleep 2
  if [[ "$RUN_API" == true ]]; then
    echo "Starting API on :$PORT ..."
    nohup uv run uvicorn investigation_agent_platform.main:app --host 0.0.0.0 --port "$PORT" > tmp/api.log 2>&1 &
    echo $! > tmp/api.pid
    echo "API pid $(cat tmp/api.pid) (log tmp/api.log)"
  fi
  if [[ "$RUN_WORKER" == true ]]; then
    echo "Starting Temporal worker ..."
    nohup uv run python -u -m investigation_agent_platform.main --worker > tmp/worker.log 2>&1 &
    echo $! > tmp/worker.pid
    echo "Worker pid $(cat tmp/worker.pid) (log tmp/worker.log)"
  fi
  echo "Application restarted. Health: curl -sf http://localhost:$PORT/api/v1/health/live"
  exit 0
fi

echo "Starting infrastructure: $COMPOSE_FILE"
docker compose -f "$COMPOSE_FILE" up -d postgres neo4j temporal temporal-ui elasticsearch kafka 

echo "Waiting for services (timeout ${TIMEOUT}s)..."
deadline=$((SECONDS + TIMEOUT))
until docker compose -f "$COMPOSE_FILE" exec -T postgres pg_isready -U iap >/dev/null 2>&1; do
  [[ $SECONDS -ge $deadline ]] && { echo "error: postgres not ready" >&2; exit 1; }
  sleep 2
done
until curl -sf http://localhost:7474 >/dev/null 2>&1; do
  [[ $SECONDS -ge $deadline ]] && { echo "warn: neo4j not reachable (ok if IAP_TOPOLOGY_ENABLED=false)"; break; }
  sleep 2
done
until curl -sf http://localhost:8233 >/dev/null 2>&1; do
  [[ $SECONDS -ge $deadline ]] && { echo "warn: temporal UI not reachable (worker needs :7233 at run time)"; break; }
  sleep 2
done
if [[ "${IAP_TOPOLOGY_REQUIRED:-false}" == "true" ]]; then
  curl -sf http://localhost:7474 >/dev/null 2>&1 || {
    echo "error: IAP_TOPOLOGY_REQUIRED=true but Neo4j is unreachable; refusing to start" >&2
    exit 1
  }
fi
echo "Infrastructure ready."

echo "Running migrations..."
uv run alembic upgrade head

if [[ "$RUN_API" == true ]]; then
  echo "Starting API on :$PORT ..."
  nohup uv run uvicorn investigation_agent_platform.main:app --host 0.0.0.0 --port "$PORT" > tmp/api.log 2>&1 &
  echo $! > tmp/api.pid
  echo "API pid $(cat tmp/api.pid) (log tmp/api.log)"
fi
if [[ "$RUN_WORKER" == true ]]; then
  echo "Starting Temporal worker ..."
  nohup uv run python -u -m investigation_agent_platform.main --worker > tmp/worker.log 2>&1 &
  echo $! > tmp/worker.pid
  echo "Worker pid $(cat tmp/worker.pid) (log tmp/worker.log)"
fi

echo "Platform up. Health: curl -sf http://localhost:$PORT/api/v1/health/live"
