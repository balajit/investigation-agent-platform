#!/bin/bash
# load-oracle-env.sh — emit `export` lines for one database from a per-app
# oracle.yaml in the EXTERNAL store. Nothing secret lives in this repo.
#
# Usage:
#   set -a; eval "$(oracle-mcp/load-oracle-env.sh --app <app> --db <d.sd.e>)"; set +a
#   # ... then: add-datasource.sh --password-env IAP_SECRET_ORACLE_<D>_<SD>_<E>_PASSWORD ...
#
# Layout: ${IAP_ORACLE_MCP_HOME:-~/workspace/docker/iap/oracle-mcp}/apps/<app>/oracle.yaml
# (see per-app-oracle.yaml.example for the schema).
#
# Fail-closed rules (any violation -> stderr + non-zero exit, NO exports):
#   - file missing, unreadable, or readable by group/other (must be 0600-ish)
#   - `app:` != --app directory name; unknown --db; empty user/password/jdbcUrl
#   - environment: prod with account_kind: personal (personal is local-only)
#   - anything outside the documented schema shape (strict subset parser, no
#     YAML dependency — python3 stdlib only, so operators need nothing extra)
#
# Env-name mapping (single source; mirrors SecretStoreSecretResolver):
#   IAP_SECRET_ORACLE_<D>_<SD>_<E>_{USER_NAME,PASSWORD,JDBC_URL}
#   dots/dashes -> _, uppercased; `jdbcUrl` normalizes to JDBC_URL.
# Plus one NON-secret informational export when `default_schema` is set:
#   IAP_ORACLE_<D>_<SD>_<E>_DEFAULT_SCHEMA (operator tooling/grants checks;
#   never an IAP_SECRET_* name — schemas are configuration, not secrets).
set -euo pipefail

IAP_ORACLE_MCP_HOME="${IAP_ORACLE_MCP_HOME:-${HOME}/workspace/docker/iap/oracle-mcp}"
APP=""; DB=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --app) APP="${2:?}"; shift 2 ;;
        --db)  DB="${2:?}"; shift 2 ;;
        -h|--help) sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; exit 2 ;;
    esac
done
[ -n "${APP}" ] || { echo "ERROR: --app is required" >&2; exit 2; }
[ -n "${DB}" ] || { echo "ERROR: --db is required (dotted, e.g. sample.orders.prod)" >&2; exit 2; }
[[ "${APP}" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "ERROR: bad --app: ${APP}" >&2; exit 2; }
[[ "${DB}" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "ERROR: bad --db: ${DB}" >&2; exit 2; }

FILE="${IAP_ORACLE_MCP_HOME}/apps/${APP}/oracle.yaml"
[ -f "${FILE}" ] || { echo "ERROR: no credential file: ${FILE}" >&2; exit 1; }
[ -r "${FILE}" ] || { echo "ERROR: credential file unreadable: ${FILE}" >&2; exit 1; }
if [ "$(stat -f '%Lp' "${FILE}" 2>/dev/null || stat -c '%a' "${FILE}")" -gt 600 ] 2>/dev/null; then
    : # stat format differs per OS; checked portably below
fi
# Portable group/other-readability check: mode's last two octal digits must be 0.
MODE="$(stat -f '%p' "${FILE}" 2>/dev/null || stat -c '%a' "${FILE}")"
# stat -f '%p' yields full mode (e.g. 100600); stat -c '%a' yields e.g. 600.
MODE="${MODE: -3}"
if [ "${MODE:1:1}" != "0" ] || [ "${MODE:2:1}" != "0" ]; then
    echo "ERROR: credential file must not be group/other-readable (chmod 0600): ${FILE}" >&2
    exit 1
fi

APP_ARG="${APP}" DB_ARG="${DB}" FILE_ARG="${FILE}" python3 - <<'PYEOF'
import os, re, sys

app, db, path = os.environ["APP_ARG"], os.environ["DB_ARG"], os.environ["FILE_ARG"]

def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)

# Strict subset parser: top-level `app:` / `environment:`, then
# `databases:` -> 2-space `<dotted>:` -> 4-space scalar fields only.
top, dbs, cur = {}, {}, None
in_dbs = False
with open(path, encoding="utf-8") as fh:
    for lineno, raw in enumerate(fh, 1):
        line = raw.rstrip("\n")
        # Strip trailing comments (" #..."); full-line comments below.
        line = re.sub(r"\s+#.*$", "", line)
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_dbs = line.strip() == "databases:"
            if not in_dbs:
                m = re.fullmatch(r"(app|environment):\s*(\S.*)?", line.strip())
                if not m:
                    fail(f"line {lineno}: unexpected top-level line")
                top[m.group(1)] = (m.group(2) or "").strip().strip("'\"")
            cur = None
        elif in_dbs and indent == 2:
            m = re.fullmatch(r"([A-Za-z0-9_.-]+):\s*", line.strip())
            if not m:
                fail(f"line {lineno}: unexpected database line")
            cur = m.group(1)
            if cur in dbs:
                fail(f"line {lineno}: duplicate database {cur!r}")
            dbs[cur] = {}
        elif in_dbs and indent == 4 and cur:
            m = re.fullmatch(r"(account_kind|user_name|password|jdbcUrl|default_schema):\s*(.*)", line.strip())
            if not m:
                fail(f"line {lineno}: unexpected field line")
            val = m.group(2).strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            dbs[cur][m.group(1)] = val
        else:
            fail(f"line {lineno}: bad indentation or placement")

if top.get("app") != app:
    fail(f"app mismatch: file says {top.get('app')!r}, asked {app!r}")
env = top.get("environment", "")
if env not in ("local", "stg", "prod"):
    fail(f"environment must be local|stg|prod (got {env!r})")
if db not in dbs:
    fail(f"unknown database {db!r} (have: {sorted(dbs)})")
rec = dbs[db]
for field in ("account_kind", "user_name", "password", "jdbcUrl"):
    if not rec.get(field):
        fail(f"{db}.{field} is missing or empty")
if rec["account_kind"] not in ("personal", "service"):
    fail(f"{db}.account_kind must be personal|service")
if env == "prod" and rec["account_kind"] == "personal":
    fail("personal accounts are local-only; prod requires account_kind: service")
schema = rec.get("default_schema", "")
if schema and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_#$]{0,127}", schema):
    fail(f"{db}.default_schema must be a plain identifier (got {schema!r})")

stem = re.sub(r"[^A-Za-z0-9]+", "_", db).strip("_").upper()
prefix = f"IAP_SECRET_ORACLE_{stem}"
out = {
    f"{prefix}_USER_NAME": rec["user_name"],
    f"{prefix}_PASSWORD": rec["password"],
    f"{prefix}_JDBC_URL": rec["jdbcUrl"],
}
if schema:
    out[f"IAP_ORACLE_{stem}_DEFAULT_SCHEMA"] = schema
for name, val in out.items():
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        fail(f"derived env name invalid: {name!r}")
    # shlex-style single-quote escaping; values never touch logs.
    print(f"export {name}='{val.replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}'")
PYEOF
