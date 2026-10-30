#!/usr/bin/env bash
# diagnosis.sh — run one battery case and print its stage information.
#
# Usage:
#   scripts/diagnosis.sh L3 [--tenant ID] [--api-base URL] [--timeout SECS]
#   scripts/diagnosis.sh --db-only <investigation-id> [--tenant ID]
#
# Default tenant is n-batt (established battery tenant: carries the
# spring-sample-service application profile). A case ID names a fixture
# in tests/fixtures/battery_cases/<ID>.json. Prints, per run:
#   1. API verdict (final status/detail)
#   2. DB stage info (investigation row, per-table counts, transitions,
#      hypotheses, evidence head, timeline tail, conclusions)
#   3. Log stage info (condensed worker-log line per reason/execute/
#      verify turn plus warnings/errors for the investigation)
#
# Requires: platform up (scripts/start-platform.sh), IAP_DATABASE_URI in
# the environment for the DB section (source .env.local first), jq.

set -u

CASE=""
if [[ $# -gt 0 && "$1" != -* ]]; then
  CASE="$1"
  shift
fi
TENANT="n-batt"
API_BASE="http://localhost:8080"
TIMEOUT=900
DB_ONLY=""

need_value() { # $1 flag, $2 value
  if [[ -z "${2:-}" || "$2" == -* ]]; then echo "error: $1 needs a value" >&2; exit 2; fi
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tenant) need_value "$1" "${2:-}"; TENANT="$2"; shift 2 ;;
    --api-base) need_value "$1" "${2:-}"; API_BASE="$2"; shift 2 ;;
    --timeout) need_value "$1" "${2:-}"; TIMEOUT="$2"; shift 2 ;;
    --db-only) need_value "$1" "${2:-}"; DB_ONLY="$2"; shift 2 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "error: unknown arg $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$CASE" && -z "$DB_ONLY" ]]; then
  echo "usage: scripts/diagnosis.sh <CASE-ID> [--tenant ID] [--api-base URL] [--timeout SECS]" >&2
  echo "   or: scripts/diagnosis.sh --db-only <investigation-id> [--tenant ID]" >&2
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

api() { # method path [body-file]
  curl -sf -H "X-Tenant-ID: $TENANT" -H "Content-Type: application/json" \
    ${3:+--data @"$3"} "$API_BASE$2" -X "$1" 2>/dev/null
}

if [[ -z "$DB_ONLY" ]]; then
  FIXTURE="$REPO_ROOT/tests/fixtures/battery_cases/$CASE.json"
  [[ -f "$FIXTURE" ]] || { echo "error: no fixture $FIXTURE" >&2; exit 2; }

  echo "== health =="
  api GET /api/v1/health/live >/dev/null || { echo "error: platform not up at $API_BASE" >&2; exit 1; }
  echo "platform UP (tenant default: $TENANT)"

  SYMPTOM="$(jq -r .symptom "$FIXTURE")"
  CONTEXT="$(jq -r .context "$FIXTURE")"
  echo "== run $CASE =="
  echo "symptom: $SYMPTOM"
  TMPBODY="$(mktemp)"
  trap 'rm -f "$TMPBODY"' EXIT
  jq -n --arg app "spring-sample-service" --arg pd "$SYMPTOM" --arg ctx "$CONTEXT" \
    '{application_id:$app, problem_description:$pd, initial_context:$ctx}' > "$TMPBODY"
  CREATE="$(api POST /api/v1/investigations "$TMPBODY")" \
    || { echo "error: create failed" >&2; exit 1; }
  IID="$(echo "$CREATE" | jq -r .id)" || exit 1
  echo "investigation: $IID"
  api POST "/api/v1/investigations/$IID/start" >/dev/null \
    || { echo "error: start failed" >&2; exit 1; }

  echo "-- polling every 10s (timeout ${TIMEOUT}s, terminal = COMPLETED/FAILED/CANCELLED) --"
  T0="$(date +%s)"
  FINAL=""
  LAST_STATUS=""
  while true; do
    RESP="$(api GET "/api/v1/investigations/$IID")" || { echo "error: poll failed" >&2; exit 1; }
    FINAL="$(echo "$RESP" | jq -c '.investigation // .')"
    STATUS="$(echo "$FINAL" | jq -r .status)"
    ELAPSED=$(( $(date +%s) - T0 ))
    if [[ "$STATUS" != "$LAST_STATUS" ]]; then
      echo "  [$ELAPSED]s $STATUS"
      LAST_STATUS="$STATUS"
    fi
    case "$STATUS" in
      COMPLETED|FAILED|CANCELLED) break ;;
    esac
    if (( ELAPSED > TIMEOUT )); then echo "TIMEOUT after ${TIMEOUT}s"; break; fi
    sleep 10
  done
  echo "settled in ${ELAPSED}s with status $STATUS"
  echo "== verdict =="
  echo "$FINAL" | jq '{id, status, phase, started_at, completed_at}'
else
  IID="$DB_ONLY"
  echo "== db-only diagnosis for $IID (tenant $TENANT) =="
fi

if [[ -z "${IAP_DATABASE_URI:-}" ]]; then
  echo "== db: SKIPPED (no IAP_DATABASE_URI; source .env.local) =="
else
  echo "== db stage info =="
  IAP_TENANT="$TENANT" IAP_IID="$IID" uv run --no-sync python - <<'EOF'
import asyncio, os

import asyncpg

async def main():
    dsn = os.environ["IAP_DATABASE_URI"]
    if dsn.startswith("postgresql+asyncpg://"):
        dsn = "postgresql://" + dsn[len("postgresql+asyncpg://"):]
    tenant, iid = os.environ["IAP_TENANT"], os.environ["IAP_IID"]
    conn = await asyncpg.connect(dsn)
    inv = await conn.fetchrow(
        "SELECT id,status,phase,application_id,created_at,started_at,completed_at,"
        " substr(problem_description,1,100) AS problem FROM investigations"
        " WHERE id=$1 AND tenant_id=$2", iid, tenant)
    if inv is None:
        print(f"no investigation row for {iid} under tenant {tenant}")
        return
    print(f"investigation {inv['id']} status={inv['status']} phase={inv['phase']} app={inv['application_id']}")
    print(f"  created={inv['created_at']} started={inv['started_at']} completed={inv['completed_at']}")
    print(f"  problem: {inv['problem']}")
    for table in ["evidence", "hypotheses", "findings", "investigation_conclusions",
                  "investigation_transitions", "timeline_events", "investigation_facts"]:
        n = await conn.fetchval(
            f"SELECT count(*) FROM {table} WHERE investigation_id=$1 AND tenant_id=$2",
            iid, tenant)
        print(f"  {table}: {n}")
    # investigation_tool_executions has no tenant column — scope by investigation only.
    n = await conn.fetchval(
        "SELECT count(*) FROM investigation_tool_executions WHERE investigation_id=$1", iid)
    print(f"  investigation_tool_executions: {n}")
    print("-- authorization profile --")
    prof = await conn.fetchrow(
        "SELECT id,status,version,profile_json FROM application_profiles"
        " WHERE tenant_id=$1 AND id=$2 ORDER BY version DESC LIMIT 1",
        tenant, inv["application_id"])
    if prof is None:
        print(f"  NO profile for app {inv['application_id']} (authorizer fail-closes: all provider calls denied)")
    else:
        print(f"  profile {prof['id']} status={prof['status']} version={prof['version']}")
        import json as _json
        pj = prof["profile_json"] or {}
        if isinstance(pj, str):
            try:
                pj = _json.loads(pj)
            except Exception:
                pj = {}
        if not isinstance(pj, dict):
            pj = {}
        inv_cfg = pj.get("investigation", {}) or {}
        for key in ("enabledEvidenceTypes", "enabled_evidence_types"):
            if key in inv_cfg:
                print(f"  investigation.{key} = {inv_cfg[key]}")
        for key in ("allowedCapabilities", "allowed_capabilities"):
            if key in pj:
                print(f"  {key} = {pj[key]}")
                break
        else:
            print("  allowedCapabilities absent (empty preserves evidence-type gate alone)")
    print("-- transitions --")
    for r in await conn.fetch(
            "SELECT timestamp,from_status,to_status,actor,substr(reason,1,90) AS reason"
            " FROM investigation_transitions WHERE investigation_id=$1"
            " ORDER BY timestamp", iid):
        print(f"  {r['timestamp']} {r['from_status']} -> {r['to_status']} [{r['actor']}] {r['reason']}")
    print("-- hypotheses --")
    for r in await conn.fetch(
            "SELECT substr(title,1,70) AS title,status,confidence_score,support_score"
            " FROM hypotheses WHERE investigation_id=$1", iid):
        print(f"  [{r['status']}] conf={r['confidence_score']} supp={r['support_score']} {r['title']}")
    print("-- evidence head --")
    for r in await conn.fetch(
            "SELECT type,source_type,substr(source_id,1,60) AS source_id,"
            " substr(summary,1,90) AS summary FROM evidence"
            " WHERE investigation_id=$1 ORDER BY retrieved_at LIMIT 12", iid):
        print(f"  {r['type']}/{r['source_type']} {r['source_id']} :: {r['summary']}")
    print("-- timeline tail --")
    for r in await conn.fetch(
            "SELECT timestamp,event_type,substr(description,1,100) AS description"
            " FROM timeline_events WHERE investigation_id=$1"
            " ORDER BY timestamp DESC LIMIT 8", iid):
        print(f"  {r['timestamp']} {r['event_type']} {r['description']}")
    print("-- conclusions --")
    for r in await conn.fetch(
            "SELECT status,substr(coalesce(root_cause,best_reason,''),1,140) AS statement"
            " FROM investigation_conclusions WHERE investigation_id=$1", iid):
        print(f"  [{r['status']}] {r['statement']}")

asyncio.run(main())
EOF
fi

echo "== log stage info (tmp/worker.log) =="
LOG="$REPO_ROOT/tmp/worker.log"
if [[ ! -f "$LOG" ]]; then
  echo "no worker log at $LOG"
  exit 0
fi
IID="$IID" python3 - "$LOG" <<'EOF'
import json, sys, os
iid = os.environ["IID"]
path = sys.argv[1]
turns = 0
matched = 0
# Two passes: completion lines carry the iteration number, decision
# lines do not. Map completion timestamp -> iteration, then attribute
# each decision to the next completion at or after its timestamp.
completions = []
raw = []
with open(path, encoding="utf-8", errors="replace") as fh:
    for line in fh:
        if iid[:8] not in line:
            continue
        matched += 1
        try:
            d = json.loads(line)
        except Exception:
            continue
        m, e = d.get("message", ""), d.get("extra", {}) or {}
        ts = str(d.get("timestamp", ""))[11:19]
        if "reason_activity completed" in m and e.get("iteration") is not None:
            completions.append((ts, e.get("iteration")))
            continue
        raw.append((ts, d.get("level", ""), m, e))
completions.sort()
for ts, level, m, e in raw:
    it = next((c[1] for c in completions if c[0] >= ts), "?")
    if "reason_activity decision" in m:
        turns += 1
        print(f"{ts} REASON it={it} {m[:130]}")
        continue
    if level in ("WARNING", "ERROR"):
        parts = []
        if e.get("error_type") or e.get("error"):
            parts.append(f"{e.get('error_type', '?')}: {e.get('error', '')}")
        ctx = "/".join(str(e.get(k, "")) for k in ("stage", "step", "outcome") if e.get(k))
        if ctx:
            parts.append(f"[{ctx}]")
        out = e.get("outputs", {}) or {}
        if out.get("degraded"):
            parts.append(f"degraded={out['degraded']}")
        if out.get("items_count") is not None:
            parts.append(f"items={out['items_count']}")
        detail = " :: ".join(p for p in parts if p)
        print(f"{ts} {level} {m[:120]}" + (f" :: {detail}" if detail else ""))
print(f"-- {turns} reason turns in log --")
if matched == 0:
    print("-- note: no lines matched; start-platform.sh --restart truncates tmp/*.log --")
EOF
