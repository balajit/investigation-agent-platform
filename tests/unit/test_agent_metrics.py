# tests/unit/test_agent_metrics.py
"""Pure-function tests for scripts/agent_metrics.py (no DB needed)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from agent_metrics import is_auto_claim_shape, parse_reason_trace


def test_parse_full_trace():
    out = parse_reason_trace(
        "Status HYPOTHESIZING -> VERIFYING: Iteration 3 verifying "
        "(reason: action=FORMULATE_HYPOTHESIS readiness=0.62 hypotheses=2 observations=5 "
        "query=[file_path=src/a.py] forced=[formulate,reread] outcome=[status=SUCCEEDED; evidence_added=3] triage=BUG)"
    )
    assert out is not None
    assert out["action"] == "FORMULATE_HYPOTHESIS"
    assert out["readiness"] == 0.62
    assert out["forced"] == ["formulate", "reread"]


def test_parse_unforced_trace():
    out = parse_reason_trace(
        "Status INVESTIGATING -> HYPOTHESIZING: Iteration 1 hypothesizing "
        "(reason: action=SEARCH_LOGS readiness=? hypotheses=0 observations=0)"
    )
    assert out is not None
    assert out["readiness"] is None
    assert out["forced"] == []


def test_parse_non_reason_row():
    assert parse_reason_trace("dispatch: QUERY_STATE executed") is None
    assert parse_reason_trace("") is None
    assert parse_reason_trace(None) is None  # type: ignore[arg-type]


def test_auto_claim_proxy():
    assert is_auto_claim_shape([]) is True
    assert is_auto_claim_shape([{"claim_kind": "CAUSAL"}]) is False
