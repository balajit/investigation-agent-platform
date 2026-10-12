#!/bin/bash
# add-datasource.sh — add one Oracle datasource to the EXTERNAL connection store.
#
# Nothing datasource-specific lives in this repo. All state (connection
# archives, encryption key) lives OUTSIDE any git checkout, located via:
#
#   IAP_ORACLE_MCP_HOME            default: ${HOME}/workspace/docker/iap/oracle-mcp
#   IAP_ORACLE_MCP_CONNECTIONS_DIR default: ${IAP_ORACLE_MCP_HOME}/connections
#   IAP_ORACLE_MCP_SECRETS_DIR     default: ${IAP_ORACLE_MCP_HOME}/secrets
#   IAP_ORACLE_MCP_KEY_FILE        default: ${IAP_ORACLE_MCP_SECRETS_DIR}/sqlcl-encryption-key
#
# Usage:
#   add-datasource.sh --name <conn-name> --user <db-user>
#                     (--tns <descriptor-or-alias> | --jdbc <jdbc-url>)
#                     [--password-env VAR | --password-file PATH]
#                     [--into <archive> | --standalone] [--skip-test] [--replace]
#
# --tns and --jdbc are alternatives: a TNS descriptor/alias, or a full custom
# JDBC string such as jdbc:oracle:thin:@(DESCRIPTION=...).
#
# Where the new connection lands:
#   --into ARCHIVE  import that archive, add the connection, re-export ALL
#                   back to it (merge into existing connections).
#   (default)       same as --into when the connections dir holds exactly one
#                   *.backup; otherwise a standalone <name>.backup export.
#   --standalone    always export just the new connection to <name>.backup.
#
# Password precedence: --password-env, --password-file, interactive prompt.
# The password is passed to SQLcl over stdin only — never argv, never logged.
#
# Needs local `sql` (SQLcl) on PATH. Note: the initial SAVE itself connects,
# so the DB must be reachable at add time; --skip-test only skips the extra
# SELECT probe.
# Quoting rules (verified against SQLcl 26.3): user and target are
# double-quoted, the password is NOT — a quoted password fails against jdbc:
# URLs with ORA-01017. Consequence: passwords containing / @ " or whitespace
# cannot be saved; the script rejects them with a clear error.
set -euo pipefail

IAP_ORACLE_MCP_HOME="${IAP_ORACLE_MCP_HOME:-${HOME}/workspace/docker/iap/oracle-mcp}"
IAP_ORACLE_MCP_CONNECTIONS_DIR="${IAP_ORACLE_MCP_CONNECTIONS_DIR:-${IAP_ORACLE_MCP_HOME}/connections}"
IAP_ORACLE_MCP_SECRETS_DIR="${IAP_ORACLE_MCP_SECRETS_DIR:-${IAP_ORACLE_MCP_HOME}/secrets}"
IAP_ORACLE_MCP_KEY_FILE="${IAP_ORACLE_MCP_KEY_FILE:-${IAP_ORACLE_MCP_SECRETS_DIR}/sqlcl-encryption-key}"

NAME=""; TNS=""; JDBC=""; DBUSER=""; PASSWORD_ENV=""; PASSWORD_FILE=""
INTO=""; STANDALONE=0; SKIP_TEST=0; REPLACE=0

usage() {
    sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//'
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --name)          NAME="${2:?}"; shift 2 ;;
        --tns)           TNS="${2:?}"; shift 2 ;;
        --jdbc)          JDBC="${2:?}"; shift 2 ;;
        --user)          DBUSER="${2:?}"; shift 2 ;;
        --password-env)  PASSWORD_ENV="${2:?}"; shift 2 ;;
        --password-file) PASSWORD_FILE="${2:?}"; shift 2 ;;
        --into)          INTO="${2:?}"; shift 2 ;;
        --standalone)    STANDALONE=1; shift ;;
        --skip-test)     SKIP_TEST=1; shift ;;
        --replace)       REPLACE=1; shift ;;
        -h|--help)       usage; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[ -n "${NAME}" ] || { echo "ERROR: --name is required" >&2; exit 2; }
[ -n "${DBUSER}" ] || { echo "ERROR: --user is required" >&2; exit 2; }
if [ -n "${TNS}" ] && [ -n "${JDBC}" ]; then
    echo "ERROR: --tns and --jdbc are mutually exclusive" >&2; exit 2
fi
[ -n "${TNS}${JDBC}" ] || { echo "ERROR: one of --tns or --jdbc is required" >&2; exit 2; }
if [ -n "${INTO}" ] && [ "${STANDALONE}" -eq 1 ]; then
    echo "ERROR: --into and --standalone are mutually exclusive" >&2; exit 2
fi
command -v sql >/dev/null 2>&1 || { echo "ERROR: SQLcl 'sql' not found on PATH" >&2; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo "ERROR: 'openssl' not found on PATH" >&2; exit 1; }

SAFE_NAME="$(printf '%s' "${NAME}" | tr -c 'A-Za-z0-9_.-' '_')"
[ "${SAFE_NAME}" = "${NAME}" ] || { echo "ERROR: --name must match [A-Za-z0-9_.-] (got: ${NAME})" >&2; exit 2; }

# Connect target: plain TNS descriptor, or a full custom JDBC string.
CONNECT_TARGET="${TNS:-${JDBC}}"

mkdir -p "${IAP_ORACLE_MCP_CONNECTIONS_DIR}"
mkdir -p "${IAP_ORACLE_MCP_SECRETS_DIR}"
chmod 0700 "${IAP_ORACLE_MCP_SECRETS_DIR}"

if [ -f "${IAP_ORACLE_MCP_KEY_FILE}" ]; then
    echo "Reusing existing encryption key."
else
    openssl rand -base64 24 > "${IAP_ORACLE_MCP_KEY_FILE}"
    chmod 0600 "${IAP_ORACLE_MCP_KEY_FILE}"
    echo "Generated new encryption key."
fi
KEY="$(cat "${IAP_ORACLE_MCP_KEY_FILE}")"

if [ -n "${PASSWORD_ENV}" ]; then
    PASSWORD="${!PASSWORD_ENV:?ERROR: env var ${PASSWORD_ENV} is unset or empty}"
elif [ -n "${PASSWORD_FILE}" ]; then
    PASSWORD="$(cat "${PASSWORD_FILE:?}")"
else
    printf 'Password for %s: ' "${DBUSER}" >&2
    stty -echo 2>/dev/null || true
    IFS= read -r PASSWORD
    stty echo 2>/dev/null || true
    printf '\n' >&2
fi
[ -n "${PASSWORD:-}" ] || { echo "ERROR: empty password" >&2; exit 2; }
case "${PASSWORD}" in
    *[/@\"[:space:]]*)
        echo "ERROR: password contains a / @ quote or whitespace character," >&2
        echo "which SQLcl cannot save (quoted passwords fail on jdbc: URLs)." >&2
        echo "Rotate to a compatible password and retry." >&2
        exit 2 ;;
esac

# Resolve merge target: explicit --into, else the single *.backup in the
# store, else standalone. Merging imports the archive first so the re-export
# carries the existing connections plus the new one.
shopt -s nullglob
STORE_BACKUPS=("${IAP_ORACLE_MCP_CONNECTIONS_DIR}"/*.backup)
MERGE_TARGET=""
if [ -n "${INTO}" ]; then
    MERGE_TARGET="${INTO}"
elif [ "${STANDALONE}" -eq 0 ] && [ "${#STORE_BACKUPS[@]}" -eq 1 ]; then
    MERGE_TARGET="${STORE_BACKUPS[0]}"
fi

if [ -n "${MERGE_TARGET}" ]; then
    [ -f "${MERGE_TARGET}" ] || { echo "ERROR: merge target not found: ${MERGE_TARGET}" >&2; exit 1; }
    echo "Merging into existing connections: ${MERGE_TARGET}"
    IMPORT_OUT="$(sql -S -nolog <<EOF 2>&1
SECRET SET K ${KEY}
CONNMGR IMPORT -KEY K -DUPLICATES REPLACE "${MERGE_TARGET}"
EXIT
EOF
)"
    echo "${IMPORT_OUT}" | grep -a "Success" >/dev/null \
        || { echo "ERROR: importing existing archive failed:" >&2; echo "${IMPORT_OUT}" >&2; exit 1; }
    OUT="${MERGE_TARGET}"
else
    OUT="${IAP_ORACLE_MCP_CONNECTIONS_DIR}/${SAFE_NAME}.backup"
    if [ -f "${OUT}" ] && [ "${REPLACE}" -ne 1 ]; then
        echo "ERROR: ${OUT} exists (pass --replace to overwrite)" >&2
        exit 1
    fi
fi

# Refuse to clobber an existing wallet connection unless --replace (which
# deletes it first so the SAVE below recreates it cleanly).
LIST_OUT="$(sql -S -nolog <<EOF 2>&1 | sed 's/\x1b\[[0-9;]*m//g'
CONNMGR LIST
EXIT
EOF
)"
NAME_RX="$(printf '%s' "${NAME}" | sed 's/\./\\./g')"
if echo "${LIST_OUT}" | grep -aEq "── ${NAME_RX}$"; then
    if [ "${REPLACE}" -eq 0 ]; then
        echo "ERROR: connection '${NAME}' already saved (pass --replace to overwrite)" >&2
        exit 1
    fi
    sql -S -nolog <<EOF >/dev/null 2>&1
CONNMGR DELETE -conn "${NAME}"
EXIT
EOF
    echo "Deleted existing connection '${NAME}' (--replace)."
fi

# SQLcl resolves its store outside $HOME, so the SAVE below lands in the
# shared wallet; the export afterwards is scoped accordingly.
# NOTE: the CONNECT line is built with printf %s (not a heredoc) so password
# metacharacters (backslash, $, backtick, !, ...) reach SQLcl byte-identical.
# User/target are double-quoted; the password is deliberately UNQUOTED
# (quoted passwords fail on jdbc: URLs — see header).
SAVE_OUT="$({
    printf 'SECRET SET K %s\n' "${KEY}"
    printf 'CONNECT -SAVE "%s" -SAVEPWD %s/%s@"%s"\n' "${NAME}" "${DBUSER}" "${PASSWORD}" "${CONNECT_TARGET}"
    printf 'CONNMGR LIST\nEXIT\n'
} | sql -S -nolog 2>&1)"
echo "${SAVE_OUT}" | grep -a -qiE "ora-|failed|error|already exists" \
    && { echo "ERROR: saving connection failed:" >&2; echo "${SAVE_OUT}" >&2; exit 1; }
echo "Connection saved."

if [ "${SKIP_TEST}" -eq 0 ]; then
    TEST_OUT="$(sql -S -nolog <<EOF 2>&1
CONNECT -NAME "${NAME}"
SELECT 'DATASOURCE-OK' AS c FROM dual;
EXIT
EOF
)"
    echo "${TEST_OUT}" | grep -a "DATASOURCE-OK" >/dev/null \
        || { echo "ERROR: live test failed (retry with --skip-test to save anyway):" >&2; echo "${TEST_OUT}" >&2; exit 1; }
    echo "Live test passed."
fi

if [ -n "${MERGE_TARGET}" ]; then
    EXPORT_OUT="$(sql -S -nolog <<EOF 2>&1
SECRET SET K ${KEY}
CONNMGR EXPORT -KEY K -OVERWRITE -ALL-CONNECTIONS "${OUT}"
EXIT
EOF
)"
else
    EXPORT_OUT="$(sql -S -nolog <<EOF 2>&1
SECRET SET K ${KEY}
CONNMGR EXPORT -KEY K -OVERWRITE "${NAME}" "${OUT}"
EXIT
EOF
)"
fi
echo "${EXPORT_OUT}" | grep -a "Successfully exported" >/dev/null \
    || { echo "ERROR: export failed:" >&2; echo "${EXPORT_OUT}" >&2; exit 1; }
chmod 0600 "${OUT}"
echo "${EXPORT_OUT}" | grep -a "Successfully exported"
echo "Exported: ${OUT}"

unset PASSWORD KEY SAVE_OUT TEST_OUT EXPORT_OUT IMPORT_OUT 2>/dev/null || true

cat <<EOF
Done. The MCP container picks this up from the external mount — restart it:
  docker compose -f oracle-mcp/docker-compose.yml run --rm -T oracle-sqlcl-mcp
Registering in the gateway (config/oracle-mcp-servers.yaml) needs a 'connection'
entry matching this name; keep that file's 'tenants' allowlist explicit.
EOF
