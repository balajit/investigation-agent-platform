#!/usr/bin/env bash
# verify_telemetry.sh — thin driver for tenant telemetry verification.
# All logic lives in the platform
# (application/onboarding/telemetry_verification.py), invoked here through
# the onboard CLI. This file carries no logic: env layering + exec only.
#
# Usage:
#   scripts/verify_telemetry.sh --telemetry-dir <dir> --tenant <id> --home-service <name>
#                              [--mapping-source-id ID] [--mapping-profiles-path DIR]
#
# Examples:
#   scripts/verify_telemetry.sh --telemetry-dir /tmp/iap-live-test --tenant tenant-a --home-service svc-a
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f ./.env.local ]]; then
    # shellcheck disable=SC1091
    set -a; source ./.env.local; set +a
fi

TELEMETRY_DIR=""
TENANT=""
HOME_SERVICE=""
MAPPING_SOURCE_ID=""
MAPPING_PROFILES_PATH=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --telemetry-dir) TELEMETRY_DIR="$2"; shift 2 ;;
        --tenant) TENANT="$2"; shift 2 ;;
        --home-service) HOME_SERVICE="$2"; shift 2 ;;
        --mapping-source-id) MAPPING_SOURCE_ID="$2"; shift 2 ;;
        --mapping-profiles-path) MAPPING_PROFILES_PATH="$2"; shift 2 ;;
        *) echo "error: unknown argument: $1" >&2; exit 2 ;;
    esac
done
if [[ -z "$TELEMETRY_DIR" ]]; then
    echo "error: --telemetry-dir is required" >&2
    exit 2
fi
if [[ -z "$TENANT" ]]; then
    echo "error: --tenant is required" >&2
    exit 2
fi
if [[ -z "$HOME_SERVICE" ]]; then
    echo "error: --home-service is required" >&2
    exit 2
fi

ARGS=(--tenant "$TENANT" verify-telemetry
    --telemetry-dir "$TELEMETRY_DIR" --home-service "$HOME_SERVICE")
if [[ -n "$MAPPING_SOURCE_ID" ]]; then
    ARGS+=(--mapping-source-id "$MAPPING_SOURCE_ID")
fi
if [[ -n "$MAPPING_PROFILES_PATH" ]]; then
    ARGS+=(--mapping-profiles-path "$MAPPING_PROFILES_PATH")
fi
exec uv run python -m investigation_agent_platform.cli.onboard "${ARGS[@]}"
