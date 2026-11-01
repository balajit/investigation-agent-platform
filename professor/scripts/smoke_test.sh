#!/bin/sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python3 "$PROJECT_ROOT/scripts/validate_project.py"
exec python3 "$PROJECT_ROOT/scripts/acceptance_tests.py"
