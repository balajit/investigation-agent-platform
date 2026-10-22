"""Regenerate pinned JSON Schemas for the onboarding agent contracts.

The design (§4) defers Formal JSON Schema to implementation: these files are
that schema. The pydantic contract models stay authoritative; this script
reproduces the pinned bytes via contracts.schemas, and the drift test
(tests/unit/test_onboarding_agent_schemas.py) fails on any divergence, so a
model change and its schema re-pinning are a single reviewable unit.

Usage: .venv/bin/python scripts/generate-onboarding-agent-schemas.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from investigation_agent_platform.application.onboarding_agent.contracts.schemas import (
    SCHEMA_MODELS,
    render_schema,
)

SCHEMAS_DIR: Final = (
    REPO_ROOT
    / "src"
    / "investigation_agent_platform"
    / "application"
    / "onboarding_agent"
    / "schemas"
)


def main() -> int:
    SCHEMAS_DIR.mkdir(parents=True, exist_ok=True)
    for name in SCHEMA_MODELS:
        (SCHEMAS_DIR / f"{name}.schema.json").write_text(
            render_schema(name), encoding="utf-8"
        )
    print(f"wrote {len(SCHEMA_MODELS)} schemas to {SCHEMAS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
