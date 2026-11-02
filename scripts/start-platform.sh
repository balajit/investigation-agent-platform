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
# Required env: IAP_DATABASE_URI, IAP_<PROVIDER>_API_KEY (per IAP_LLM_PROVIDER)
# Optional env: IAP_TOPOLOGY_* (see docs/IAP-implementation-part7-knowledge-v1.md D7),
#   PORT (default 8080), COMPOSE_FILE (default docker/docker-compose.yml).
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker/docker-compose.yml}"
PORT="${PORT:-8080}"
TIMEOUT="${TIMEOUT:-120}"
OPERATOR_HOME="${IAP_OPERATOR_HOME:-${HOME}/workspace/.iap}"
OPERATOR_SCOPE=""
RUN_API=true
RUN_WORKER=true
RESTART_ONLY=false

# `uv run` wrappers exit on SIGTERM but orphan the real child process,
# which keeps holding the port. So pid-file kills alone are not enough:
# sweep the distinctive command lines too. Workers stuck in bridge I/O
# may ignore SIGTERM, hence the SIGKILL fallback.
stop_app() {
  [[ -f logs/api.pid ]] && kill "$(cat logs/api.pid)" 2>/dev/null || true
  [[ -f logs/worker.pid ]] && kill "$(cat logs/worker.pid)" 2>/dev/null || true
  pkill -f "uvicorn investigation_agent_platform.main:app" 2>/dev/null || true
  pkill -f "investigation_agent_platform.main --worker" 2>/dev/null || true
  sleep 2
  pkill -9 -f "uvicorn investigation_agent_platform.main:app" 2>/dev/null || true
  pkill -9 -f "investigation_agent_platform.main --worker" 2>/dev/null || true
  rm -f logs/api.pid logs/worker.pid
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
    --port=*) PORT="${arg#--port=}" ;;
    --compose-file=*) COMPOSE_FILE="${arg#--compose-file=}" ;;
    --operator-home=*) OPERATOR_HOME="${arg#--operator-home=}" ;;
    --operator-scope=*) OPERATOR_SCOPE="${arg#--operator-scope=}" ;;
    --help|-h)
      echo "Usage: $0 [--no-api] [--no-worker] [--restart] [--stop] [--timeout=SECS] [--compose-file=PATH] [--port=PORT] [--operator-home=PATH] [--operator-scope=SCOPE]"
      exit 0
      ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# Operator overlay (optional, DEV/single-scope convenience — NOT the production
# topology: one API+worker fleet serves ALL scopes, resolving tenant/app
# credentials per request; booting one stack per scope does not scale past a
# handful of services. This flag exists so a developer can run one scope
# locally). Sources the scope's app.env BEFORE the required-var gates so
# overlays may provide them. Absent scope = current behavior, never an error.
if [[ -n "${OPERATOR_SCOPE}" ]]; then
  if [[ ! "${OPERATOR_SCOPE}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || [[ "${OPERATOR_SCOPE}" == *..* ]]; then
    echo "error: bad --operator-scope (want <org>/<app>/<env>): ${OPERATOR_SCOPE}" >&2
    exit 2
  fi
  _org="${OPERATOR_SCOPE%%/*}"; _rest="${OPERATOR_SCOPE#*/}"; _app="${_rest%%/*}"; _env="${_rest#*/}"
  # Lever -> leaf merge (leaf wins), mirroring iap-operator.py env resolution.
  for _overlay in "${OPERATOR_HOME}/scopes/${_org}/${_env}/app.env" "${OPERATOR_HOME}/scopes/${OPERATOR_SCOPE}/app.env"; do
    if [[ -f "${_overlay}" ]]; then
      echo "Sourcing operator overlay: ${_overlay}"
      set -a; source "${_overlay}"; set +a
    fi
  done
  if [[ ! -d "${OPERATOR_HOME}/scopes/${OPERATOR_SCOPE}" ]]; then
    echo "warn: operator scope not found (continuing without overlays): ${OPERATOR_SCOPE}" >&2
  fi
fi
export IAP_OPERATOR_HOME="${OPERATOR_HOME}"
export IAP_OPERATOR_SCOPE="${OPERATOR_SCOPE}"
if [[ -z "${IAP_DATABASE_URI:-}" ]]; then
  echo "error: IAP_DATABASE_URI must be set" >&2
  exit 2
fi
# S3 least-privilege dev roles: per-process DSNs, each defaulting to the
# single IAP_DATABASE_URI. Operator sets IAP_API_DATABASE_URI
# (iap_app), IAP_WORKER_DATABASE_URI (iap_worker), and
# IAP_MIGRATION_DATABASE_URI (iap_migrator) in the secret environment;
# the superuser DSN stays available for operator tooling only.
IAP_API_DSN="${IAP_API_DATABASE_URI:-$IAP_DATABASE_URI}"
IAP_WORKER_DSN="${IAP_WORKER_DATABASE_URI:-$IAP_DATABASE_URI}"
IAP_MIGRATION_DSN="${IAP_MIGRATION_DATABASE_URI:-$IAP_DATABASE_URI}"
provider_key="IAP_$(echo "${IAP_LLM_PROVIDER:-openai}" | tr '[:lower:]' '[:upper:]')_API_KEY"
if [[ -z "${!provider_key:-}" ]]; then
  echo "error: ${provider_key} must be set for IAP_LLM_PROVIDER=${IAP_LLM_PROVIDER:-openai}" >&2
  exit 2
fi
if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "error: compose file not found: $COMPOSE_FILE" >&2
  exit 2
fi
command -v docker >/dev/null || { echo "error: docker is required" >&2; exit 2; }

mkdir -p tmp logs

# Console-log layout (dev): static names under logs/; the rotating file
# handler owns hourly bucketing for structured records, while these
# shell redirects stay restart-rotated below. Pid files hold the process
# ID: logs/api.pid and logs/worker.pid.
API_PID="logs/api.pid"
WORKER_PID="logs/worker.pid"

# Rotate (never truncate) the application console logs before each launch
# so a --restart cannot destroy the evidence of the run that just ended.
# The previous log is archived as logs/<name>-<MMDDYY>.log (UTC); a second
# restart the same day falls back to logs/<name>-<MMDDYY>-<HHMMSS>.log.
# The newest IAP_LOG_KEEP archives per name are retained (default 10).
rotate_logs() {
  local name keep="${IAP_LOG_KEEP:-10}" stamp archive old
  stamp="$(date -u +%m%d%y)"
  for name in "$@"; do
    if [[ -s "logs/${name}.log" ]]; then
      archive="logs/${name}-${stamp}.log"
      if [[ -e "$archive" ]]; then
        archive="logs/${name}-${stamp}-$(date -u +%H%M%S).log"
      fi
      mv "logs/${name}.log" "$archive"
    fi
    # Newest first by mtime; drop all but the newest $keep archives.
    # shellcheck disable=SC2012
    { ls -t "logs/${name}-"*.log 2>/dev/null || true; } | tail -n +"$((keep + 1))" | while read -r old; do
      rm -f -- "$old"
    done
  done
}

# --restart: bounce the application processes only. Infrastructure
# (compose services) and migrations are left untouched.
if [[ "$RESTART_ONLY" == true ]]; then
  echo "Restarting application (API/worker)..."
  stop_app
  sleep 2
  [[ "$RUN_API" == true ]] && rotate_logs api
  [[ "$RUN_WORKER" == true ]] && rotate_logs worker
  if [[ "$RUN_API" == true ]]; then
    echo "Starting API on :$PORT ..."
    IAP_DATABASE_URI="$IAP_API_DSN" nohup uv run uvicorn investigation_agent_platform.main:app --host 0.0.0.0 --port "$PORT" >> logs/api.log 2>&1 &
    echo $! > "$API_PID"
    echo "API pid $(cat "$API_PID") (log logs/api.log, pid $API_PID)"
  fi
  if [[ "$RUN_WORKER" == true ]]; then
    echo "Starting Temporal worker ..."
    IAP_DATABASE_URI="$IAP_WORKER_DSN" nohup uv run python -u -m investigation_agent_platform.main --worker >> logs/worker.log 2>&1 &
    echo $! > "$WORKER_PID"
    echo "Worker pid $(cat "$WORKER_PID") (log logs/worker.log, pid $WORKER_PID)"
  fi
  echo "Application restarted. Health: curl -sf http://localhost:$PORT/api/v1/health/live"
  exit 0
fi

echo "Starting infrastructure: $COMPOSE_FILE"
docker compose -f "$COMPOSE_FILE" up -d postgres neo4j temporal temporal-ui elasticsearch kafka filebeat postgres-mcp-iap redis otel-collector

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
until curl -sf http://localhost:13133/ >/dev/null 2>&1; do
  [[ $SECONDS -ge $deadline ]] && { echo "warn: otel-collector not reachable (spans will not export)"; break; }
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
# Sanctioned dev-stack use of the dev database (port 5452): inline override
# with reason, never exported. Disposable databases need no override.
IAP_PG_GUARD_OVERRIDE="start-platform.sh dev-stack migration on the dev database" IAP_DATABASE_URI="$IAP_MIGRATION_DSN" uv run alembic upgrade head

[[ "$RUN_API" == true ]] && rotate_logs api
[[ "$RUN_WORKER" == true ]] && rotate_logs worker
if [[ "$RUN_API" == true ]]; then
  echo "Starting API on :$PORT ..."
  IAP_DATABASE_URI="$IAP_API_DSN" nohup uv run uvicorn investigation_agent_platform.main:app --host 0.0.0.0 --port "$PORT" >> logs/api.log 2>&1 &
  echo $! > "$API_PID"
  echo "API pid $(cat "$API_PID") (log logs/api.log, pid $API_PID)"
fi
if [[ "$RUN_WORKER" == true ]]; then
  echo "Starting Temporal worker ..."
  IAP_DATABASE_URI="$IAP_WORKER_DSN" nohup uv run python -u -m investigation_agent_platform.main --worker >> logs/worker.log 2>&1 &
  echo $! > "$WORKER_PID"
  echo "Worker pid $(cat "$WORKER_PID") (log logs/worker.log, pid $WORKER_PID)"
fi

echo "Platform up. Health: curl -sf http://localhost:$PORT/api/v1/health/live"
