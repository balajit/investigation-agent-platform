"""Shadow telemetry pass-through in _derive_loop_telemetry.

The stamped loop_telemetry is gate evidence: this pins that shadow keys
survive shaping (capped, type-checked), malformed entries degrade
gracefully without dropping comparable turns, and runs without a hook
produce no shadow keys (back-compat).
"""

from __future__ import annotations

from investigation_agent_platform.application.worker.activities import (
    _derive_loop_telemetry,
)

_COUNTS = {"hypotheses_formulated": 0, "hypotheses_bound": 0, "hypotheses_verified": 0}


def _turn(**over: object) -> dict:
    base: dict[str, object] = {
        "iteration": 0,
        "recorded": "SEARCH_CODE",
        "selected": "CORRELATE",
        "match": False,
        "cause": "rank-disagreement",
        "shadow_terminal": False,
        "evidence_banked_total": 3,
        "prev_outcome": "SUCCEEDED",
        "params": {"file_path": "a.py"},
        "forced": [],
        "owed": {"floor": False},
    }
    base.update(over)
    return base  # type: ignore[return-value]


def test_shadow_keys_pass_through() -> None:
    passed = {
        "llm_tokens_total": 100,
        "shadow_trace": [_turn(), _turn(iteration=1, match=True)],
        "shadow_summary": {
            "turns": 2,
            "matched": 1,
            "capture_errors": 0,
            "eligible": 2,
            "diverged": 1,
        },
    }
    out = _derive_loop_telemetry(passed, dict(_COUNTS), 0)
    assert out is not None
    assert out["llm_tokens_total"] == 100
    assert len(out["shadow_trace"]) == 2
    assert out["shadow_trace"][0]["forced"] == []
    assert out["shadow_trace"][0]["owed"] == {"floor": False}
    assert out["shadow_summary"]["diverged"] == 1

def test_malformed_entries_degrade_without_dropping_turns() -> None:
    passed = {
        "shadow_trace": [
            _turn(params="not-a-dict"),
            _turn(forced="not-a-list"),
            _turn(iteration=True),
            "not-a-dict",
            _turn(),
        ],
        "shadow_summary": {
            "turns": 1,
            "matched": 0,
            "capture_errors": 0,
            "eligible": 1,
            "diverged": 1,
        },
    }
    out = _derive_loop_telemetry(passed, dict(_COUNTS), 0)
    assert out is not None
    trace = out["shadow_trace"]
    # Bool iteration + non-dict entries are skipped (uncomparable);
    # malformed optionals are coerced, keeping the turn comparable.
    assert len(trace) == 3
    assert trace[0]["params"] == {}
    assert trace[1]["forced"] == []
    assert trace[2]["recorded"] == "SEARCH_CODE"
    assert out["shadow_summary"]["turns"] == 1


def test_no_shadow_keys_without_hook() -> None:
    out = _derive_loop_telemetry({"llm_tokens_total": 5}, dict(_COUNTS), 3)
    assert out is not None
    assert "shadow_trace" not in out and "shadow_summary" not in out
    assert out["llm_tokens_total"] == 5
    assert out["evidence_banked_repo"] == 3


def test_trace_capped() -> None:
    passed = {"shadow_trace": [_turn(iteration=i) for i in range(300)]}
    out = _derive_loop_telemetry(passed, dict(_COUNTS), 0)
    assert out is not None
    assert len(out["shadow_trace"]) == 256
