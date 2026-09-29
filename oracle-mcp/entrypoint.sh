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
CONN_FILES=(/config/*.json)
if [ "${#CONN_FILES[@]}" -eq 0 ]; then
    echo "ERROR: no connection files in /config" >&2
    exit 1
fi

if [ ! -f "${KEY_FILE}" ]; then
    echo "ERROR: SQLcl encryption key not found." >&2
    exit 1
fi

SQLCL_KEY="$(cat "${KEY_FILE}")"
export SQLCL_KEY

for CONN_FILE in "${CONN_FILES[@]}"; do
    echo "Importing $(basename "${CONN_FILE}")..." >&2
    sql -nolog <<EOF >&2
CONNMGR IMPORT  -DUPLICATES REPLACE ${CONN_FILE}
EXIT
EOF
done
unset SQLCL_KEY

echo "Connections:" >&2
sql -nolog <<EOF >&2
CONNMGR LIST
EXIT
EOF

echo "Starting SQLcl MCP Server..." >&2

exec sql -mcp
