"""H5 remainder: D6 may not force CONCLUDE before evidence was attempted (lv4).

Rule: the opinion-only fast path (D6) must not force a CONCLUDE while the run has
banked zero rows AND dispatched no gathering fetch. The invariant is "no
abstention-for-lack-of-evidence before attempting evidence", so it covers turn 0
and any pathological never-banked later turn without special-casing. Once the
loop has tried (a fetch was dispatched, or a row is banked), an all-NOT_CHECKABLE
turn may abstain as before.

Decision recorded with this change: the identifier/path floor stays EXCLUDED
(see test_naming_code_still_does_not_defeat_nc in test_h5_checkability_floor).

Version-pinned (lv4): rows pinned lv2/lv3 keep the old behavior. Fail-open: any
fault in the new counting leaves the cap inert (behaves as before).
"""

from __future__ import annotations

import inspect

import pytest

from investigation_agent_platform.application.worker.workflows import run_investigation
from investigation_agent_platform.application.worker.workflows.forcing import (
    _d6_decision,
    _is_gathering_action,
    _should_force_conclude_not_checkable,
)
from investigation_agent_platform.domain.investigation.loop_version import (
    LOOP_VERSION_CURRENT,
    UnknownLoopVersion,
    loop_version_at_least,
)

ALL_NC = {
    "hypotheses_evaluated": ["The implementation is beautiful"],
    "claims": [{"statement": "The implementation is beautiful", "claim_kind": "NOT_CHECKABLE"}],
}
CHECKABLE = {
    "hypotheses_evaluated": ["Foo is defined in Foo.java"],
    "claims": [{"statement": "Foo is defined in Foo.java", "claim_kind": "DEFINITION"}],
}


def _decide(reason_data, *, banked=0, fetches=0, version="lv4", is_defer=False):
    return _d6_decision(
        reason_data,
        is_defer=is_defer,
        evidence_banked_total=banked,
        fetches_dispatched=fetches,
        loop_version=version,
    )


# --- the version pin -------------------------------------------------------------------------


def test_lv4_is_current_and_includes_earlier_semantics() -> None:
    assert LOOP_VERSION_CURRENT == "lv4"
    assert loop_version_at_least({"loop_version": "lv4"}, "lv4") is True
    assert loop_version_at_least({}, "lv4") is True  # unmarked resolves to current
    assert loop_version_at_least({"loop_version": "lv3"}, "lv4") is False
    assert loop_version_at_least({"loop_version": "lv2"}, "lv4") is False
    assert loop_version_at_least({"loop_version": "lv4"}, "lv3") is True
    with pytest.raises(UnknownLoopVersion):
        loop_version_at_least({"loop_version": "lv99"}, "lv4")


# --- the gate, each cell of the matrix --------------------------------------------------------------


def test_turn_zero_all_nc_with_nothing_banked_does_not_force() -> None:
    assert _decide(ALL_NC, banked=0, fetches=0) == (False, True)


def test_turn_zero_all_nc_with_banked_rows_still_forces() -> None:
    assert _decide(ALL_NC, banked=3, fetches=0) == (True, False)


def test_later_turn_no_rows_and_no_fetch_does_not_force() -> None:
    # Not literal turn 0: the invariant is "no evidence attempted yet".
    assert _decide(ALL_NC, banked=0, fetches=0) == (False, True)


def test_after_fetches_with_zero_banked_all_nc_forces_the_abstention() -> None:
    # The loop tried (an empty fetch) - an honest abstention is now allowed.
    assert _decide(ALL_NC, banked=0, fetches=2) == (True, False)


def test_a_single_dispatched_fetch_is_enough() -> None:
    assert _decide(ALL_NC, banked=0, fetches=1) == (True, False)


def test_a_checkable_turn_is_never_forced_and_never_counted_as_blocked() -> None:
    assert _decide(CHECKABLE, banked=0, fetches=0) == (False, False)
    assert _decide(CHECKABLE, banked=5, fetches=5) == (False, False)


def test_a_defer_turn_never_forces_and_is_not_counted_as_blocked() -> None:
    assert _decide(ALL_NC, banked=0, fetches=0, is_defer=True) == (False, False)


# --- version routing --------------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["lv2", "lv3"])
def test_older_pins_keep_the_old_behavior(version: str) -> None:
    assert _decide(ALL_NC, banked=0, fetches=0, version=version) == (True, False)


def test_lv4_and_newer_are_gated() -> None:
    assert _decide(ALL_NC, banked=0, fetches=0, version="lv4") == (False, True)


def test_an_unrecognized_pin_leaves_the_cap_inert() -> None:
    # Fail-open: behave as today rather than guess a version.
    assert _decide(ALL_NC, banked=0, fetches=0, version="lv99") == (True, False)


# --- fail-open on malformed counting ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("banked", "fetches"),
    [(None, 0), (0, None), (None, None), ("x", 0), (0, "x"), (-1, 0), (0, -1), (float("nan"), 0)],
)
def test_malformed_counts_leave_the_cap_inert(banked, fetches) -> None:
    assert _decide(ALL_NC, banked=banked, fetches=fetches) == (True, False)


def test_boolean_counts_are_not_numbers() -> None:
    assert _decide(ALL_NC, banked=False, fetches=False) == (True, False)


@pytest.mark.parametrize("data", [None, {}, {"hypotheses_evaluated": []}, "nope", 5])
def test_malformed_reason_data_never_forces_or_blocks(data) -> None:
    assert _decide(data, banked=0, fetches=0) == (False, False)


# --- the underlying predicate is unchanged ------------------------------------------------------------


def test_the_bare_predicate_keeps_its_old_signature_and_behavior() -> None:
    assert _should_force_conclude_not_checkable(ALL_NC) is True
    assert _should_force_conclude_not_checkable(CHECKABLE) is False
    assert _should_force_conclude_not_checkable({}) is False


def test_the_predicate_cap_is_a_separate_decision_after_the_match() -> None:
    assert (
        _should_force_conclude_not_checkable(
            ALL_NC, evidence_banked_total=0, fetches_dispatched=0, cap_enabled=True
        )
        is False
    )
    assert (
        _should_force_conclude_not_checkable(
            ALL_NC, evidence_banked_total=0, fetches_dispatched=0, cap_enabled=False
        )
        is True
    )
    assert (
        _should_force_conclude_not_checkable(
            ALL_NC, evidence_banked_total=0, fetches_dispatched=1, cap_enabled=True
        )
        is True
    )


# --- what counts as a fetch -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("SEARCH_CODE", True),
        ("GET_CODE", True),
        ("QUERY_STATE", True),
        ("search_code", True),
        ("FORMULATE_HYPOTHESIS", False),
        ("VERIFY_HYPOTHESIS", False),
        ("CONCLUDE", False),
        ("", False),
        (None, False),
    ],
)
def test_only_gathering_actions_count_as_a_fetch(action, expected) -> None:
    assert _is_gathering_action(action) is expected


# --- seam-level: D6 fires post-fetch but not pre-fetch ----------------------------------------------------


def test_over_a_run_d6_fires_after_the_loop_tried_but_not_before() -> None:
    """Replays the workflow's bookkeeping for a model that keeps answering all-NC."""
    banked = 0
    fetches = 0
    fired_on: list[int] = []
    blocked_on: list[int] = []
    for turn in range(4):
        fire, blocked = _decide(ALL_NC, banked=banked, fetches=fetches)
        (fired_on if fire else blocked_on).append(turn)
        if fire:
            break
        # The cap let the cascade proceed to gather; the fetch came back empty.
        fetches += 1
    assert blocked_on == [0], "blocked exactly once, before any fetch"
    assert fired_on == [1], "fires on the next turn, after the loop tried"


# --- workflow wiring --------------------------------------------------------------------------------------


def test_the_workflow_routes_d6_through_the_decision_and_counts_the_block() -> None:
    source = inspect.getsource(run_investigation)
    assert "_d6_decision(" in source, "the call site must use the capped decision"
    assert "_is_gathering_action(" in source, "dispatched fetches must be counted"
    assert "d6_blocked_no_evidence_count" in source, "the gate's firing rate must be measurable"
    assert "fetches_dispatched" in source


def test_gathering_set_matches_the_action_enum() -> None:
    from investigation_agent_platform.application.worker.workflows.forcing import (
        _GATHERING_ACTIONS,
    )
    from investigation_agent_platform.domain.investigation.models import ActionType

    terminal = {"FORMULATE_HYPOTHESIS", "VERIFY_HYPOTHESIS", "CONCLUDE"}
    assert {a.value for a in ActionType} - terminal == set(_GATHERING_ACTIONS)
