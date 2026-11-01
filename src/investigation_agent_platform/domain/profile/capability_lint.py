# src/investigation_agent_platform/domain/profile/capability_lint.py
"""Profile lint: which claim kinds a profile cannot investigate (R9-D4).

Pure: reads the R6 policy table through ``unavailable_actions`` and reports
each claim kind whose every acquiring action is unavailable. Advisory only —
surfaced as non-blocking warnings where profiles are validated, never as an
authorization decision.
"""

from __future__ import annotations

from typing import Any

from investigation_agent_platform.domain.finding.models import CLAIM_ACQUIRING_ACTIONS
from investigation_agent_platform.domain.profile.capability_policy import unavailable_actions


def lint_profile_capabilities(profile: Any) -> list[str]:
    """Claim kinds the profile cannot investigate, as warning strings."""
    try:
        unavailable = set(unavailable_actions(profile))
    except Exception:
        return []
    warnings: list[str] = []
    for kind, acquiring in CLAIM_ACQUIRING_ACTIONS.items():
        try:
            if acquiring and acquiring <= unavailable:
                warnings.append(
                    f"claims of kind {kind.value} cannot be investigated: "
                    f"{sorted(acquiring)} are all unavailable"
                )
        except Exception:
            continue
    return warnings
