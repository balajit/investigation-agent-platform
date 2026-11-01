"""D4/GAP.07: every workflow loop_telemetry key is stamped or explicitly exempt.

Parses the loop_telemetry dict literal in run_investigation.py and fails when
a key is neither in the stamp allowlist nor in an explicit exemption set, so
the next dropped key is a test failure, not a measurement gap. (Adapted from
the design: the allowlist lives in forcing_rules.telemetry_allowlist, and
dedicated-block keys in TELEMETRY_HANDLED_ELSEWHERE.)
"""

from __future__ import annotations

import ast
from pathlib import Path

from investigation_agent_platform.application.worker.activities import inputs
from investigation_agent_platform.domain.investigation.forcing_rules import (
    TELEMETRY_DECLARED_NOT_STAMPED,
    TELEMETRY_HANDLED_ELSEWHERE,
    telemetry_allowlist,
)

_WORKFLOW = Path(inputs.__file__).resolve().parents[1] / "workflows" / "run_investigation.py"


def _workflow_loop_telemetry_keys() -> set[str]:
    tree = ast.parse(_WORKFLOW.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.keyword)
            and node.arg == "loop_telemetry"
            and isinstance(node.value, ast.Dict)
        ):
            return {
                k.value for k in node.value.keys if isinstance(k, ast.Constant)
            }
    raise AssertionError("loop_telemetry literal not found")


def test_every_workflow_telemetry_key_is_stamped_or_explicitly_exempt() -> None:
    allowed = (
        set(telemetry_allowlist())
        | set(TELEMETRY_HANDLED_ELSEWHERE)
        | set(TELEMETRY_DECLARED_NOT_STAMPED)
    )
    missing = _workflow_loop_telemetry_keys() - allowed
    assert not missing, (
        f"loop_telemetry keys dropped by _derive_loop_telemetry: {sorted(missing)}"
    )


def test_t320_cap_firing_rate_keys_are_stamped() -> None:
    """T3.20: the lv4 cap's firing-rate keys reach the stamped telemetry."""
    out = inputs._derive_loop_telemetry(
        {"fetches_dispatched": 3, "d6_blocked_no_evidence_count": 1}, {}, 0
    )
    assert out is not None
    assert out["fetches_dispatched"] == 3
    assert out["d6_blocked_no_evidence_count"] == 1
