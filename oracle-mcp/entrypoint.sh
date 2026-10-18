#!/bin/bash
# Entrypoint: validate mounts, import every connections/*.json, start sql -mcp.
# Multi-DB: one JSON per database; each imports under its own connection name.
# Diagnostics to stderr only — stdout carries MCP protocol traffic.
set -euo pipefail

KEY_FILE="/run/secrets/sqlcl-encryption-key"

echo "Starting Oracle SQLcl MCP Server..." >&2

echo "SQLcl version:" >&2
sql -version >&2

echo "Java version:" >&2
java -version 2>&1 | head -1 >&2

shopt -s nullglob
CONN_FILES=(/config/*.json /config/*.backup)
if [ "${#CONN_FILES[@]}" -eq 0 ]; then
    echo "ERROR: no connection files found in /config" >&2
    exit 1
fi

if [ ! -f "${KEY_FILE}" ]; then
    echo "ERROR: SQLcl encryption key not found at ${KEY_FILE}" >&2
    exit 1
fi

# Load key into memory and register emergency cleanup trap
SQLCL_KEY="$(cat "${KEY_FILE}")"
export SQLCL_KEY
trap 'unset SQLCL_KEY' EXIT

# Register the key as a secret so encrypted archives (*.backup from
# CONNMGR EXPORT) can be imported with -KEY. Plain SQL Developer JSON
# imports do not need it.
# NOTE: SECRET SET and all IMPORTs run in ONE sql invocation — secrets live
# only for the session that created them, so separate invocations cannot see
# the key. Commands are piped over stdin (never argv), then the key is unset.
SECRET_NAME="CONN_KEY"
echo "Importing connection configurations..." >&2

{
    printf 'SECRET SET %s %s\n' "${SECRET_NAME}" "${SQLCL_KEY}"
    for CONN_FILE in "${CONN_FILES[@]}"; do
        echo "Importing $(basename "${CONN_FILE}")..." >&2
        case "${CONN_FILE}" in
            *.backup)
                printf 'CONNMGR IMPORT -KEY "%s" -DUPLICATES REPLACE "%s"\n' "${SECRET_NAME}" "${CONN_FILE}"
                ;;
            *)
                printf 'CONNMGR IMPORT -DUPLICATES REPLACE "%s"\n' "${CONN_FILE}"
                ;;
        esac
    done
    printf 'EXIT\n'
} | sql -nolog >&2

# Explicitly unset key after import loop completes
unset SQLCL_KEY
trap - EXIT

echo "Active SQLcl Connections:" >&2
sql -nolog <<EOF >&2
CONNMGR LIST
EXIT
EOF

echo "Starting SQLcl MCP Server on stdout..." >&2

# Final handover to MCP server daemon mode
exec sql -mcp
