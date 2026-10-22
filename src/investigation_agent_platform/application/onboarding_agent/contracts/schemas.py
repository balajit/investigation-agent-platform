# src/investigation_agent_platform/application/onboarding_agent/contracts/schemas.py
"""Pinned JSON Schema registry for the onboarding agent contracts.

The design (§4) defers Formal JSON Schema to implementation: the generated
``schemas/*.schema.json`` files are that schema. The pydantic models stay
authoritative; :func:`render_schema` reproduces the pinned bytes exactly, and
the drift test fails on any divergence. Model change and schema re-pinning
are a single reviewable unit — regenerate with
``scripts/generate-onboarding-agent-schemas.py``.
"""

from __future__ import annotations

import json
from typing import Final

from pydantic import BaseModel

from investigation_agent_platform.application.onboarding_agent.contracts.coverage import (
    CoverageReport,
)
from investigation_agent_platform.application.onboarding_agent.contracts.guidance import (
    CitedAnswer,
    CoverageQuestion,
)
from investigation_agent_platform.application.onboarding_agent.contracts.linkage import (
    ReportLinkage,
)
from investigation_agent_platform.application.onboarding_agent.contracts.registry import (
    RegistryEntry,
)
from investigation_agent_platform.application.onboarding_agent.contracts.sniffer import (
    BuildMapOutput,
    CensusOutput,
    CiMapOutput,
    DepScanOutput,
    HygieneOutput,
    SizerOutput,
)
from investigation_agent_platform.application.onboarding_agent.contracts.watchdog import (
    Diagnosis,
    WatchdogSnapshot,
)

SCHEMA_MODELS: Final[dict[str, type[BaseModel]]] = {
    "coverage-report": CoverageReport,
    "report-linkage": ReportLinkage,
    "watchdog-snapshot": WatchdogSnapshot,
    "diagnosis": Diagnosis,
    "coverage-question": CoverageQuestion,
    "cited-answer": CitedAnswer,
    "census-output": CensusOutput,
    "buildmap-output": BuildMapOutput,
    "cimap-output": CiMapOutput,
    "depscan-output": DepScanOutput,
    "hygiene-output": HygieneOutput,
    "sizer-output": SizerOutput,
    "registry-entry": RegistryEntry,
}


def render_schema(name: str) -> str:
    """Render the pinned bytes for one schema. Deterministic: sorted keys,
    two-space indent, single trailing newline."""
    payload = SCHEMA_MODELS[name].model_json_schema(mode="validation")
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
