# tests/unit/test_f9_observability.py
"""F9: one counter map, lossless telemetry, typed observability lists (Sonnet F D2-D5)."""

import ast
import pathlib

from investigation_agent_platform.domain.investigation.forcing_rules import (
    COUNTER_KEYS,
    TELEMETRY_DECLARED_NOT_STAMPED,
    TELEMETRY_HANDLED_ELSEWHERE,
    counter_telemetry,
    fired_labels,
    forced_stamp,
    merge_counts,
    new_counters,
    payload_stamp,
    shadow_forced,
    shadow_forcing_used,
    telemetry_allowlist,
    telemetry_key,
)

WORKFLOW = pathlib.Path(
    "src/investigation_agent_platform/application/worker/workflows/run_investigation.py"
)


def _loop_telemetry_literal_keys() -> set[str]:
    tree = ast.parse(WORKFLOW.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "loop_telemetry" and isinstance(
            node.value, ast.Dict
        ):
            return {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    raise AssertionError("loop_telemetry literal not found")


def test_every_literal_workflow_telemetry_key_is_stamped_or_declared() -> None:
    literal = _loop_telemetry_literal_keys()
    # **counter_telemetry(counters) spread is dynamic; its keys are the allowlist minus scalars.
    dynamic = {telemetry_key(k) for k in COUNTER_KEYS}
    known = telemetry_allowlist() | TELEMETRY_HANDLED_ELSEWHERE | set(
        TELEMETRY_DECLARED_NOT_STAMPED
    )
    # literal non-counter keys must be known; counter keys arrive via spread.
    static_literal = {k for k in literal if k not in dynamic}
    assert static_literal <= known, f"workflow sends keys the stamp would drop: {sorted(static_literal - known)}"


def test_counter_map_round_trip() -> None:
    counters = new_counters()
    assert set(counters) == set(COUNTER_KEYS)
    counters["pivot"] = 2
    tel = counter_telemetry(counters)
    assert tel["forced_pivot_count"] == 2
    assert tel["forced_floor_count"] == 0
    merge_counts(counters, {"pivot": 5, "bogus": 1})
    assert counters["pivot"] == 5
    merge_counts(counters, {"pivot": -1})
    assert counters["pivot"] == 5
    merge_counts(counters, None)
    assert counters["pivot"] == 5


def test_telemetry_allowlist_covers_formerly_dropped_keys() -> None:
    allow = telemetry_allowlist()
    for key in (
        "d6_blocked_no_evidence_count",
        "fetches_dispatched",
        "forced_pivot_count",
        "forced_floor_count",
        "forced_callsite_count",
        "forced_absence_disconfirmation_count",
        "forced_smallest_fetch_definition_count",
    ):
        assert key in allow, key


def test_derive_stamp_is_lossless_for_allowlisted_keys() -> None:
    from investigation_agent_platform.application.worker.activities.inputs import (
        _derive_loop_telemetry,
    )

    passed = {k: 1 for k in telemetry_allowlist()}
    passed.update({"triage_route": "BUG", "plan": [1, 2]})
    out = _derive_loop_telemetry(passed, {}, 0)
    assert out is not None
    for k in telemetry_allowlist():
        assert out.get(k) == 1, k
    assert "plan" not in out


def test_typed_lists_derive_from_force() -> None:
    rd = {"forced_walk_stack": True, "forced_pivot_recollect": True, "forced_formulate": True}
    assert set(fired_labels(rd)) == {"walk_stack", "pivot_recollect", "formulate"}
    stamp = forced_stamp(rd)
    assert stamp["forced_walk_stack"] is True and stamp["forced_floor"] is False
    pay = payload_stamp({**rd, "forced_smallest_fetch_kind": "DEFINITION"})
    assert pay["forced_smallest_fetch_kind"] == "DEFINITION"
    assert shadow_forced({**rd, "forced_formulate_rule": "synthesis"}) == sorted(
        ["formulate", "pivot_recollect", "synthesis", "walk_stack"]
    )


def test_shadow_mapping_separates_synthesis_under_lv8() -> None:
    counters = new_counters()
    counters["formulate"] = 2
    counters["synthesis"] = 3
    old = shadow_forcing_used(counters, synthesis_separate=False)
    new = shadow_forcing_used(counters, synthesis_separate=True)
    assert old["synthesis"] == 2
    assert new["synthesis"] == 3 and new["formulate"] == 2
