# tests/unit/test_f4_pivot_bounds.py
"""F4: the pivot is bounded, non-destructive and anchored on the model's own focus (lv7).

Three defects, each reproduced before the fix:

A. No per-run cap. Twelve distinct-file proposals produced eleven pivots; every
   other forcing rule caps at two.
B. Destructive rewrite. A SEARCH_CODE proposal carrying a query and a pinned
   revision was dispatched as a whole-file GET_CODE at HEAD: the query was dropped
   and the revision pin (the incident revision) was lost.
C. Shift was measured against the dispatched focus. A forced fetch on file A made
   the model's next, unrelated proposal look like a pivot, and the pivot starved
   the carried requery order.

From lv7: the pivot caps at two per run (a pivot cancelled by walk-stack is not
counted), preserves the proposal's parameters and revision (a scoped SEARCH_CODE is
left alone), and the next shift is measured against the model's own previous
proposal. lv6 and older pins keep the old behavior.
"""

from __future__ import annotations

import pytest

from investigation_agent_platform.application.worker.workflows import forcing
from investigation_agent_platform.application.worker.workflows.forcing import (
    PIVOT_RECOLLECT_CAP,
    PostReasonCascadeState,
    _pivot_hardened,
    _should_force_pivot_recollect,
    apply_post_reason_cascade,
)
from investigation_agent_platform.domain.investigation.loop_version import (
    LOOP_VERSION_CURRENT,
    loop_version_at_least,
)

PREV = {"surface": "svc/old.py", "surface_kind": "file", "line_start": None, "line_end": None}


def _proposal(action, **params):  # type: ignore[no-untyped-def]
    return {"next_action": action, "action_parameters": params}


def _pivot(reason, *, hardened=True, forced_count=0, prev=PREV, targets=None):  # type: ignore[no-untyped-def]
    return _should_force_pivot_recollect(
        prev,
        reason,
        10,
        targets or [],
        preserve_proposal=hardened,
        forced_count=forced_count,
    )


# --- version routing ------------------------------------------------------------------------------


def test_lv7_is_current_and_includes_lv6() -> None:
    assert LOOP_VERSION_CURRENT == "lv9"
    assert loop_version_at_least({"loop_version": "lv7"}, "lv6") is True
    assert loop_version_at_least({"loop_version": "lv6"}, "lv7") is False


@pytest.mark.parametrize(
    ("pin", "expected"),
    [("lv7", True), (None, True), ("", True), ("lv6", False), ("lv5", False), ("lv2", False)],
)
def test_the_pivot_is_hardened_from_lv7(pin, expected) -> None:  # type: ignore[no-untyped-def]
    assert _pivot_hardened(pin) is expected


def test_an_unrecognized_pin_keeps_the_old_pivot() -> None:
    assert _pivot_hardened("lv99") is False
    assert _pivot_hardened(object()) is False


# --- B: non-destructive ---------------------------------------------------------------------------


def test_the_old_pivot_rewrote_a_scoped_search_and_lost_the_revision_pin() -> None:
    reason = _proposal("SEARCH_CODE", query="validateOrder", file_path="svc/new.py", revision="abc1234")
    old = _pivot(reason, hardened=False)
    assert old is not None
    assert old["next_action"] == "GET_CODE"
    assert old["action_parameters"] == {"file_path": "svc/new.py", "revision": "HEAD"}


def test_a_scoped_search_with_a_query_is_left_as_proposed() -> None:
    reason = _proposal("SEARCH_CODE", query="validateOrder", file_path="svc/new.py", revision="abc1234")
    assert _pivot(reason) is None, "the model's own scoped search already targets the surface"


def test_a_proposed_revision_pin_survives_the_pivot() -> None:
    reason = _proposal("GET_CODE", file_path="svc/new.py", start_line=5, end_line=9, revision="abc1234")
    out = _pivot(reason)
    assert out is not None
    assert out["action_parameters"]["revision"] == "abc1234"
    assert out["action_parameters"]["start_line"] == 5
    assert out["action_parameters"]["end_line"] == 9


def test_head_is_only_the_default_when_no_revision_was_proposed() -> None:
    out = _pivot(_proposal("GET_CODE", file_path="svc/new.py"))
    assert out is not None
    assert out["action_parameters"]["revision"] == "HEAD"


def test_other_proposed_parameters_are_preserved() -> None:
    reason = _proposal("GET_CODE", file_path="svc/new.py", read_mode="range", repository="r1")
    out = _pivot(reason)
    assert out is not None
    assert out["action_parameters"]["read_mode"] == "range"
    assert out["action_parameters"]["repository"] == "r1"


def test_a_queryless_file_scoped_search_still_becomes_a_read() -> None:
    out = _pivot(_proposal("SEARCH_CODE", file_path="svc/new.py", revision="abc1234"))
    assert out is not None
    assert out["next_action"] == "GET_CODE"
    assert out["action_parameters"]["revision"] == "abc1234"


def test_a_symbol_pivot_keeps_a_proposed_revision() -> None:
    out = _pivot(_proposal("FIND_SYMBOL", symbol_name="normalize", revision="abc1234"))
    assert out is not None
    assert out["next_action"] == "FIND_SYMBOL"
    assert out["action_parameters"]["revision"] == "abc1234"


def test_the_proposal_is_never_mutated() -> None:
    reason = _proposal("GET_CODE", file_path="svc/new.py", revision="abc1234")
    before = {"next_action": reason["next_action"], "action_parameters": dict(reason["action_parameters"])}
    _pivot(reason)
    assert reason == before


def test_the_old_symbol_and_line_shapes_are_unchanged_for_minimal_proposals() -> None:
    sym = _pivot(_proposal("FIND_SYMBOL", symbol_name="normalize"))
    assert sym is not None
    assert sym["action_parameters"] == {"symbol_name": "normalize", "revision": "HEAD"}
    fil = _pivot(_proposal("GET_CODE", file_path="svc/new.py", start_line=10, end_line=20))
    assert fil is not None
    assert fil["action_parameters"] == {
        "file_path": "svc/new.py",
        "start_line": 10,
        "end_line": 20,
        "revision": "HEAD",
    }


# --- A: the per-run cap -----------------------------------------------------------------------------


def test_the_cap_matches_the_other_forcing_rules() -> None:
    assert PIVOT_RECOLLECT_CAP == 2


def test_the_rule_stands_down_at_the_cap_only_when_hardened() -> None:
    reason = _proposal("GET_CODE", file_path="svc/new.py")
    assert _pivot(reason, forced_count=PIVOT_RECOLLECT_CAP - 1) is not None
    assert _pivot(reason, forced_count=PIVOT_RECOLLECT_CAP) is None
    assert _pivot(reason, forced_count=PIVOT_RECOLLECT_CAP + 5) is None
    assert _pivot(reason, hardened=False, forced_count=PIVOT_RECOLLECT_CAP + 5) is not None


@pytest.mark.parametrize("bad", [None, "x", -1, float("nan")])
def test_a_malformed_count_does_not_block_the_pivot(bad) -> None:  # type: ignore[no-untyped-def]
    assert _pivot(_proposal("GET_CODE", file_path="svc/new.py"), forced_count=bad) is not None


def _state(reason, history, focus, loop_version, counts=None, **over):  # type: ignore[no-untyped-def]
    base = PostReasonCascadeState(
        reason_data=reason,
        action_history=history,
        evidence_banked_total=5,
        triage_route="BUG",
        description="where is the order filter implemented",
        knowledge_seed={},
        max_iterations=40,
        iteration=3,
        prev_focus=focus,
        is_defer=False,
        d6_fired=False,
        absence_forced=False,
        counts=counts or {},
        formulated_count=1,
        tenant_id="t",
        investigation_id="i",
        loop_version=loop_version,
    )
    for key, value in over.items():
        setattr(base, key, value)
    return base


def _walk(loop_version, turns=12):  # type: ignore[no-untyped-def]
    focus, history, counts, pivots = None, [], {}, 0
    for n in range(turns):
        reason = _proposal("GET_CODE", file_path=f"svc/f{n}.py", start_line=1, end_line=20)
        out = apply_post_reason_cascade(_state(reason, history, focus, loop_version, counts))
        focus, counts = out.prev_focus, out.counts
        pivots += 1 if out.reason_data.get("forced_pivot_recollect") else 0
        history.append(
            {
                "action": out.reason_data["next_action"],
                "evidence_added": 1,
                "parameters": out.reason_data["action_parameters"],
            }
        )
    return pivots, counts


def test_twelve_distinct_files_used_to_pivot_eleven_times() -> None:
    pivots, counts = _walk("lv6")
    assert pivots == 11, "premise: an lv6 run has no cap"
    assert "pivot" not in counts


def test_from_lv7_the_pivot_stops_at_the_cap_and_counts_in_the_run_counters() -> None:
    pivots, counts = _walk("lv7")
    assert pivots == PIVOT_RECOLLECT_CAP
    assert counts["pivot"] == PIVOT_RECOLLECT_CAP


def test_a_capped_turn_dispatches_the_models_own_proposal() -> None:
    reason = _proposal("SEARCH_CODE", query="q", file_path="svc/zzz.py", revision="abc1234")
    out = apply_post_reason_cascade(
        _state(reason, [], PREV, "lv7", {"pivot": PIVOT_RECOLLECT_CAP})
    )
    assert out.reason_data["next_action"] == "SEARCH_CODE"
    assert out.reason_data["action_parameters"]["revision"] == "abc1234"
    assert not out.reason_data.get("forced_pivot_recollect")


def test_a_pivot_cancelled_by_walk_stack_is_not_counted() -> None:
    frames = [{"file": "svc/Frame.java", "line": 10, "symbol": "m"}]
    reason = _proposal("GET_CODE", file_path="svc/new.py", start_line=1, end_line=5)
    out = apply_post_reason_cascade(
        _state(reason, [], PREV, "lv7", knowledge_seed={"frames": frames, "error_class": "E"})
    )
    assert out.reason_data.get("forced_walk_stack") is True
    assert out.reason_data.get("forced_pivot_recollect") is False
    assert out.counts.get("pivot", 0) == 0


# --- C: shift is measured against the model's own focus ------------------------------------------------


def _requery_carrier():  # type: ignore[no-untyped-def]
    return {
        "kind": "work_order",
        "order_kind": "REQUERY",
        "iteration": 1,
        "turn_issued": 1,
        "claim_ref": "abc",
        "query": "SampleOrderFilter",
        "scope": "",
        "max_attempts": 3,
        "consumed": False,
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


def _forced_absence_turn(loop_version, focus):  # type: ignore[no-untyped-def]
    """A turn whose dispatch was forced (absence disconfirmation reads file F)."""
    reason = {
        **_proposal("GET_CODE", file_path="svc/forced.py", start_line=1, end_line=30),
        "forced_absence_disconfirmation": True,
    }
    return apply_post_reason_cascade(
        _state(reason, [], focus, loop_version, absence_forced=True)
    )


def test_a_forced_fetch_used_to_become_the_pivot_anchor() -> None:
    a = {"surface": "svc/a.py", "surface_kind": "file", "line_start": None, "line_end": None}
    out = _forced_absence_turn("lv6", a)
    assert out.prev_focus["surface"] == "svc/forced.py", "premise: lv6 anchors on the dispatch"


def test_from_lv7_a_forced_fetch_does_not_move_the_anchor() -> None:
    a = {"surface": "svc/a.py", "surface_kind": "file", "line_start": None, "line_end": None}
    out = _forced_absence_turn("lv7", a)
    assert out.prev_focus["surface"] == "svc/a.py"


def _next_turn(loop_version, focus):  # type: ignore[no-untyped-def]
    """The model re-proposes file A while a REQUERY order waits in the history."""
    reason = _proposal("GET_CODE", file_path="svc/a.py", start_line=1, end_line=20)
    history = [{"action": "GET_CODE", "evidence_added": 1, "parameters": {}}, _requery_carrier()]
    return apply_post_reason_cascade(_state(reason, history, focus, loop_version))


def test_a_forced_fetch_no_longer_starves_the_requery_order() -> None:
    a = {"surface": "svc/a.py", "surface_kind": "file", "line_start": None, "line_end": None}
    focus_after_forced = {
        "lv6": _forced_absence_turn("lv6", a).prev_focus,
        "lv7": _forced_absence_turn("lv7", a).prev_focus,
    }
    old = _next_turn("lv6", focus_after_forced["lv6"])
    new = _next_turn("lv7", focus_after_forced["lv7"])
    assert old.reason_data.get("forced_pivot_recollect") is True, "premise: pivot starved the order"
    assert not old.reason_data.get("forced_requery")
    assert not new.reason_data.get("forced_pivot_recollect")
    assert new.reason_data.get("forced_requery") is True


def test_the_anchor_still_follows_the_models_own_proposals() -> None:
    reason = _proposal("GET_CODE", file_path="svc/new.py", start_line=1, end_line=20)
    out = apply_post_reason_cascade(_state(reason, [], PREV, "lv7"))
    assert out.prev_focus["surface"] == "svc/new.py"


def test_a_pivot_cancelled_by_walk_stack_leaves_the_anchor_so_it_can_refire() -> None:
    frames = [{"file": "svc/Frame.java", "line": 10, "symbol": "m"}]
    reason = _proposal("GET_CODE", file_path="svc/new.py", start_line=1, end_line=5)
    out = apply_post_reason_cascade(
        _state(reason, [], PREV, "lv7", knowledge_seed={"frames": frames, "error_class": "E"})
    )
    assert out.prev_focus["surface"] == "svc/old.py"


# --- wiring ---------------------------------------------------------------------------------------------------


def test_the_workflow_maps_the_pivot_counter_back_from_the_cascade() -> None:
    from investigation_agent_platform.domain.investigation.forcing_rules import (
        counter_telemetry,
        merge_counts,
        new_counters,
    )

    counters = new_counters()
    counters["pivot"] = 2
    tel = counter_telemetry(counters)
    assert tel["forced_pivot_count"] == 2
    returned = {"pivot": 3, "walk_stack": 1}
    merge_counts(counters, returned)
    assert counters["pivot"] == 3 and counters["walk_stack"] == 1


def test_forcing_exports_the_cap() -> None:
    assert hasattr(forcing, "PIVOT_RECOLLECT_CAP")
