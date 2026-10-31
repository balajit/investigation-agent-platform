# tests/unit/test_f2_carrier_separation.py
"""F2: work-order carriers must not be read as turns (lv6).

Verify-turn carriers (REQUERY orders, GAP_FILL feeds) live in `action_history`
beside the real turn entries. Readers that mean "the last N turns" index the list
positionally, so a trailing carrier displaced a turn: the two-empty replan trigger
read [turn, carrier] and never fired, the pivot-damping window shrank, the reasoner
prompt and verify activity saw carrier rows as blank turns, `actions_used` counted
carriers, and the shadow `prev_outcome` read a carrier.

From lv6 the turn-oriented readers see `turn_entries(history)`. Carrier readers
(order dispatch, coverage stamps, consumed stamps) keep the full list, because they
index into it. Older pins keep the old behavior.
"""

from __future__ import annotations

import inspect

import pytest

from investigation_agent_platform.application.worker.workflows import forcing, run_investigation
from investigation_agent_platform.application.worker.workflows.forcing import (
    CARRIER_KINDS,
    PostReasonCascadeState,
    _carriers_separated,
    _recent_collection_surfaces,
    _should_force_replan,
    apply_post_reason_cascade,
    is_carrier_entry,
    turn_entries,
)
from investigation_agent_platform.domain.investigation.loop_version import (
    LOOP_VERSION_CURRENT,
    loop_version_at_least,
)


def _turn(action: str, evidence_added: int = 0, **extra):  # type: ignore[no-untyped-def]
    return {"action": action, "evidence_added": evidence_added, "parameters": {}, **extra}


def _requery_carrier():  # type: ignore[no-untyped-def]
    return {
        "kind": "work_order",
        "order_kind": "REQUERY",
        "iteration": 2,
        "turn_issued": 2,
        "consumed": False,
    }


def _gap_carrier():  # type: ignore[no-untyped-def]
    return {
        "kind": "work_order_feed",
        "order_kind": "GAP_FILL",
        "iteration": 2,
        "turn_issued": 2,
        "orders": [],
    }


# --- the pure view ---------------------------------------------------------------------------


def test_the_carrier_kinds_match_what_the_workflow_appends() -> None:
    assert frozenset({"work_order", "work_order_feed"}) == CARRIER_KINDS
    assert _requery_carrier()["kind"] in CARRIER_KINDS
    assert _gap_carrier()["kind"] in CARRIER_KINDS


def test_is_carrier_entry() -> None:
    assert is_carrier_entry(_requery_carrier()) is True
    assert is_carrier_entry(_gap_carrier()) is True
    assert is_carrier_entry(_turn("GET_CODE")) is False
    assert is_carrier_entry({}) is False
    for junk in (None, "x", 5, []):
        assert is_carrier_entry(junk) is False


def test_turn_entries_drops_carriers_and_keeps_turn_order() -> None:
    t1, t2, t3 = _turn("A"), _turn("B"), _turn("C")
    history = [t1, _requery_carrier(), t2, _gap_carrier(), t3, _gap_carrier()]
    assert turn_entries(history) == [t1, t2, t3]


@pytest.mark.parametrize("value", [None, [], ()])
def test_turn_entries_of_nothing_is_empty(value) -> None:  # type: ignore[no-untyped-def]
    assert turn_entries(value) == []


def test_turn_entries_ignores_non_dict_items_and_never_mutates() -> None:
    history = [_turn("A"), "junk", None, _requery_carrier()]
    before = list(history)
    assert turn_entries(history) == [history[0]]
    assert history == before


def test_a_real_turn_entry_never_carries_a_carrier_kind() -> None:
    """The workflow's turn entry has no `kind` key; guard the assumption."""
    source = inspect.getsource(run_investigation)
    start = source.index("action_history.append(\n                {\n                    \"iteration\": iteration,")
    block = source[start : start + 1800]
    assert '"kind"' not in block


# --- version routing ------------------------------------------------------------------------------


def test_lv6_is_current_and_includes_lv5() -> None:
    assert LOOP_VERSION_CURRENT == "lv6"
    assert loop_version_at_least({"loop_version": "lv6"}, "lv5") is True
    assert loop_version_at_least({"loop_version": "lv5"}, "lv6") is False


@pytest.mark.parametrize(
    ("pin", "expected"),
    [("lv6", True), (None, True), ("", True), ("lv5", False), ("lv4", False), ("lv2", False)],
)
def test_carriers_are_separated_from_lv6(pin, expected) -> None:  # type: ignore[no-untyped-def]
    assert _carriers_separated(pin) is expected


def test_an_unrecognized_pin_keeps_the_old_behavior() -> None:
    assert _carriers_separated("lv99") is False
    assert _carriers_separated(object()) is False


# --- the replan trigger (the review's named symptom) -------------------------------------------------

PROPOSAL = {"next_action": "SEARCH_CODE", "action_parameters": {"query": "x"}}
DESCRIPTION = "where is the order filter implemented"


def _replan(history, separate):  # type: ignore[no-untyped-def]
    return _should_force_replan(
        DESCRIPTION, PROPOSAL, history, 0, separate_carriers=separate
    )


def test_replan_fires_on_two_empty_turns_without_a_carrier() -> None:
    history = [_turn("SEARCH_CODE"), _turn("SEARCH_CODE")]
    assert _replan(history, False) is not None
    assert _replan(history, True) is not None


@pytest.mark.parametrize("carrier", [_requery_carrier, _gap_carrier])
def test_a_trailing_carrier_used_to_disable_replan(carrier) -> None:  # type: ignore[no-untyped-def]
    history = [_turn("SEARCH_CODE"), _turn("SEARCH_CODE"), carrier()]
    assert _replan(history, False) is None, "premise: the old reader loses the trigger"
    assert _replan(history, True) is not None, "lv6 sees the two real turns"


def test_a_carrier_between_the_two_turns_also_no_longer_hides_them() -> None:
    history = [_turn("SEARCH_CODE"), _gap_carrier(), _turn("SEARCH_CODE")]
    assert _replan(history, False) is None
    assert _replan(history, True) is not None


def test_separation_does_not_make_replan_fire_when_a_turn_banked_evidence() -> None:
    history = [_turn("SEARCH_CODE", 2), _turn("SEARCH_CODE"), _gap_carrier()]
    assert _replan(history, True) is None


def test_separation_does_not_make_replan_fire_for_different_actions() -> None:
    history = [_turn("SEARCH_CODE"), _turn("GET_CODE"), _gap_carrier()]
    assert _replan(history, True) is None


def test_a_single_real_turn_plus_carriers_is_still_too_little_history() -> None:
    history = [_turn("SEARCH_CODE"), _requery_carrier(), _gap_carrier()]
    assert _replan(history, True) is None


# --- the pivot damping window --------------------------------------------------------------------------


def _read(path):  # type: ignore[no-untyped-def]
    return {"action": "GET_CODE", "evidence_added": 1, "parameters": {"file_path": path}}


def test_carriers_no_longer_push_a_collected_file_out_of_the_damping_window() -> None:
    history = [_read("svc/a.py")] + [_gap_carrier() for _ in range(10)]
    assert "file:svc/a.py" not in _recent_collection_surfaces(history, 10), "premise"
    assert "file:svc/a.py" in _recent_collection_surfaces(history, 10, separate_carriers=True)


def test_the_damping_window_still_bounds_real_turns() -> None:
    history = [_read(f"svc/f{i}.py") for i in range(12)]
    out = _recent_collection_surfaces(history, 10, separate_carriers=True)
    assert "file:svc/f0.py" not in out
    assert "file:svc/f11.py" in out


# --- through the real cascade -----------------------------------------------------------------------------


def _state(history, loop_version):  # type: ignore[no-untyped-def]
    return PostReasonCascadeState(
        reason_data=dict(PROPOSAL),
        action_history=history,
        evidence_banked_total=2,
        triage_route="BUG",
        description=DESCRIPTION,
        knowledge_seed={},
        max_iterations=10,
        iteration=4,
        prev_focus=None,
        is_defer=False,
        d6_fired=False,
        absence_forced=False,
        counts={},
        formulated_count=1,
        tenant_id="tenant-a",
        investigation_id="inv-1",
        loop_version=loop_version,
    )


def test_the_cascade_replans_through_a_trailing_carrier_only_from_lv6() -> None:
    history = [_turn("SEARCH_CODE"), _turn("SEARCH_CODE"), _gap_carrier()]
    old = apply_post_reason_cascade(_state(list(history), "lv5"))
    new = apply_post_reason_cascade(_state(list(history), "lv6"))
    assert not old.reason_data.get("forced_replan"), "an lv5 run keeps the old behavior"
    assert new.reason_data.get("forced_replan") is True
    assert new.reason_data["next_action"] == "SEARCH_LOGS"


def test_the_cascade_still_dispatches_a_carried_requery_order() -> None:
    """Carrier readers keep the full list (they index into it)."""
    carrier = {
        **_requery_carrier(),
        "claim_ref": "abc",
        "query": "SampleOrderFilter",
        "scope": "",
        "max_attempts": 3,
        "order": {
            "work_order_id": "00000000-0000-0000-0000-000000000001",
            "kind": "REQUERY",
            "action_type": "SEARCH_CODE",
            "action_parameters": {"query": "SampleOrderFilter"},
            "addresses": {
                "triage_route": "BUG",
                "claim_kind": "untyped",
                "has_unbeaten_rival": False,
            },
            "cost_estimate": 1,
            "max_attempts": 3,
            "seed_source": "test:carrier",
            "status": "PENDING",
        },
    }
    history = [_turn("SEARCH_CODE", 1), carrier]
    out = apply_post_reason_cascade(_state(history, "lv6"))
    assert out.reason_data.get("forced_requery"), out.reason_data
    assert out.consumed_order_ref is not None


# --- workflow wiring -----------------------------------------------------------------------------------------


def test_the_workflow_reads_turns_through_the_view_for_prompts_counts_and_prev_outcome() -> None:
    source = inspect.getsource(run_investigation)
    assert source.count("_turn_view(") >= 4, "two recent_actions sites, actions_used, prev_outcome"
    assert "recent_actions=_turn_view()[-10:]" in source
    assert "actions_used=len(_turn_view())" in source
    assert "_prev = _turn_view()[-1] if _turn_view() else {}" in source
    assert "_carriers_separated(" in source


def test_carrier_appends_and_in_place_stamps_still_use_the_full_list() -> None:
    source = inspect.getsource(run_investigation)
    assert "action_history.append(_requery_order)" in source
    assert "action_history.append(_gap_carrier)" in source
    assert 'action_history[-1]["forced_requery"]' in source


def test_forcing_module_exports_the_helpers() -> None:
    assert hasattr(forcing, "turn_entries") and hasattr(forcing, "is_carrier_entry")
