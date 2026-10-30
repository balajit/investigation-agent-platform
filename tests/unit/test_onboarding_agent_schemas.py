"""Pinned JSON Schema drift tests for the onboarding agent contracts.

Each file in application/onboarding_agent/schemas must byte-match what the
contract models render. A failure here means a model changed without its
schema being re-pinned: regenerate with
scripts/generate-onboarding-agent-schemas.py and review both together.
"""

from pathlib import Path

from investigation_agent_platform.application.onboarding_agent.contracts.schemas import (
    SCHEMA_MODELS,
    render_schema,
)

SCHEMAS_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "src"
    / "investigation_agent_platform"
    / "application"
    / "onboarding_agent"
    / "schemas"
)


def test_schema_files_match_registered_models() -> None:
    pinned = sorted(path.name for path in SCHEMAS_DIR.glob("*.schema.json"))
    expected = sorted(f"{name}.schema.json" for name in SCHEMA_MODELS)
    assert pinned == expected, (
        "schema directory drift: regenerate with scripts/generate-onboarding-agent-schemas.py"
    )


def test_schema_bytes_match_rendered_models() -> None:
    for name in SCHEMA_MODELS:
        pinned = (SCHEMAS_DIR / f"{name}.schema.json").read_text(encoding="utf-8")
        assert pinned == render_schema(name), f"schema drift: {name}"
        assert pinned.endswith("\n") and not pinned.endswith("\n\n")
