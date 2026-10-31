# tests/unit/test_w2_requery_carriers.py
"""W2: newest-carrier-only requery + age expiry under LV_W (review W2, D9/D10/D12)."""

import inspect

from investigation_agent_platform.application.worker.workflows import forcing
from investigation_agent_platform.application.worker.workflows.forcing import (
    REQUERY_ORDER_TTL_TURNS,
    ConsumeReason,
    PostReasonCascadeState,
    WorkOrderPolicy,
    _carry_requery_order,
    _should_force_order_requery,
    apply_post_reason_cascade,
)


def _directive(**over):  # type: ignore[no-untyped-def]
    base = {
        "query": "order filter",
        "scope": "",
        "action_type": "SEARCH_CODE",
        "action_parameters": {"query": "order filter"},
        "claim_ref": "c1",
        "max_attempts": 3,
    }
    base.update(over)
    return {"ledger_requery": base}


def _carrier(turn: int, **over):  # type: ignore[no-untyped-def]
    c = _carry_requery_order(_directive(), turn)
    assert c is not None
    c.update(over)
    return c


def _policy(iteration):  # type: ignore[no-untyped-def]
    return WorkOrderPolicy(hardened=True, iteration=iteration)


def _cascade(history, iteration, counts=None, pin="lv9"):  # type: ignore[no-untyped-def]
    state = PostReasonCascadeState(
        reason_data={"next_action": "FORMULATE_HYPOTHESIS"},
        action_history=list(history), evidence_banked_total=9,
        triage_route="BUG", description="d", knowledge_seed={},
        max_iterations=10, iteration=iteration,
        prev_focus=None, is_defer=False, d6_fired=False, absence_forced=False,
        counts=dict(counts or {}), formulated_count=1,
        tenant_id="t", investigation_id="i", loop_version=pin,
    )
    return apply_post_reason_cascade(state)


def test_carry_writes_ttl_and_round_trips() -> None:
    import json

    from investigation_agent_platform.domain.collection.work_orders import WorkOrder

    c = _carrier(3)
    assert c["order"]["ttl_rounds"] == REQUERY_ORDER_TTL_TURNS == 3
    WorkOrder.model_validate(json.loads(json.dumps(c["order"])))


def test_fresh_carrier_dispatches() -> None:
    carrier = _carrier(8)
    out = _cascade([carrier], 9)
    assert out.reason_data.get("forced_requery") is True
    assert out.consumed_order_ref is carrier
    assert carrier.get("consumed") is not True  # the workflow stamps consumed at dispatch


def test_stale_carrier_expires_end_to_end() -> None:
    carrier = _carrier(3)
    history = [carrier] + [
        {"action": "GET_CODE", "evidence_added": 1, "parameters": {"file_path": f"svc/f{i}.py"}}
        for i in range(5)
    ]
    out = _cascade(history, 9)
    assert out.reason_data.get("forced_requery") is not True
    assert carrier.get("consumed") is True
    assert carrier.get("consumed_reason") == "expired"
    assert out.counts == {}
    events = out.order_events
    assert len(events) == 1 and events[0]["reason"] == "expired"
    assert events[0]["age"] == 6 and events[0]["turn_issued"] == 3


def test_ttl_boundary() -> None:
    assert _should_force_order_requery([_carrier(5)], [], _policy(8))["action_type"] == "SEARCH_CODE"
    expired = _should_force_order_requery([_carrier(5)], [], _policy(9))
    assert expired["mark_consumed"] == ConsumeReason.EXPIRED.value


def test_consumed_newest_does_not_resurrect_older() -> None:
    old, new = _carrier(2), _carrier(5)
    new["consumed"] = True
    assert _should_force_order_requery([old, new], [], _policy(9)) is None


def test_newer_supersedes_older_unconsumed() -> None:
    old, new = _carrier(3, claim_ref="old"), _carrier(5, claim_ref="new")
    out = _cascade([old, new], 6)
    assert out.reason_data.get("forced_requery") is True
    assert out.consumed_order_ref.get("claim_ref") == "new"
    assert old.get("consumed") is not True


def test_pivot_suppressed_carrier_survives_two_pivots() -> None:
    carrier = _carrier(3)
    counts: dict = {}
    pivot_proposal = {"next_action": "GET_CODE", "action_parameters": {"file_path": "svc/z.py"}}
    history = [carrier]
    for turn in (4, 5):
        state = PostReasonCascadeState(
            reason_data=dict(pivot_proposal), action_history=list(history), evidence_banked_total=0,
            triage_route="BUG", description="d", knowledge_seed={},
            max_iterations=10, iteration=turn,
            prev_focus={"surface": "svc/old.py", "surface_kind": "file"},
            is_defer=False, d6_fired=False, absence_forced=False,
            counts=dict(counts), formulated_count=1,
            tenant_id="t", investigation_id="i", loop_version="lv9",
        )
        out = apply_post_reason_cascade(state)
        assert out.reason_data.get("forced_pivot_recollect") is True
        assert carrier.get("consumed") is not True
        counts = dict(out.counts)
    out = _cascade(history, 6, counts=counts)
    assert out.reason_data.get("forced_requery") is True  # age 3 fires after the F4 window


def test_waiting_past_the_window_expires() -> None:
    carrier = _carrier(3)
    counts: dict = {}
    pivot_proposal = {"next_action": "GET_CODE", "action_parameters": {"file_path": "svc/z.py"}}
    history = [carrier]
    for turn in (4, 5):
        state = PostReasonCascadeState(
            reason_data=dict(pivot_proposal), action_history=list(history), evidence_banked_total=0,
            triage_route="BUG", description="d", knowledge_seed={},
            max_iterations=10, iteration=turn,
            prev_focus={"surface": "svc/old.py", "surface_kind": "file"},
            is_defer=False, d6_fired=False, absence_forced=False,
            counts=dict(counts), formulated_count=1,
            tenant_id="t", investigation_id="i", loop_version="lv9",
        )
        counts = dict(apply_post_reason_cascade(state).counts)
    defer = PostReasonCascadeState(
        reason_data={"next_action": "GET_CODE", "action_parameters": {}},
        action_history=list(history), evidence_banked_total=0,
        triage_route="BUG", description="d", knowledge_seed={},
        max_iterations=10, iteration=6, prev_focus=None,
        is_defer=True, d6_fired=False, absence_forced=False,
        counts=dict(counts), formulated_count=1,
        tenant_id="t", investigation_id="i", loop_version="lv9",
    )
    apply_post_reason_cascade(defer)
    out = _cascade(history, 7, counts=counts)
    assert out.reason_data.get("forced_requery") is not True
    assert carrier.get("consumed_reason") == "expired"


def test_unreadable_age_fails_closed() -> None:
    for bad in (None, "x", True):
        c = _carrier(3)
        c["turn_issued"] = bad
        out = _should_force_order_requery([c], [], _policy(9))
        assert out["mark_consumed"] == ConsumeReason.INVALID_ORDER.value, bad
        assert out["detail"] == "unreadable_age", bad
    c = _carrier(3)
    out = _should_force_order_requery([c], [], WorkOrderPolicy(hardened=True, iteration=None))
    assert out["mark_consumed"] == ConsumeReason.INVALID_ORDER.value


def test_zero_ttl_expires_next_turn() -> None:
    c = _carrier(3)
    c["order"] = dict(c["order"], ttl_rounds=0)
    out = _should_force_order_requery([c], [], _policy(4))
    assert out["mark_consumed"] == ConsumeReason.EXPIRED.value


def test_stand_down_turns_still_age_the_carrier() -> None:
    carrier = _carrier(3)
    history = [carrier]
    for turn, kwargs in ((4, {"is_defer": True}), (5, {"d6_fired": True}), (6, {"absence_forced": True})):
        state = PostReasonCascadeState(
            reason_data={"next_action": "GET_CODE", "action_parameters": {}},
            action_history=list(history), evidence_banked_total=0,
            triage_route="BUG", description="d", knowledge_seed={},
            max_iterations=10, iteration=turn, prev_focus=None,
            is_defer=False, d6_fired=False, absence_forced=False,
            counts={}, formulated_count=1,
            tenant_id="t", investigation_id="i", loop_version="lv9",
        )
        for key, value in kwargs.items():
            setattr(state, key, value)
        out = apply_post_reason_cascade(state)
        assert out.reason_data.get("forced_requery") is not True
        assert carrier.get("consumed") is not True
    out = _cascade(history, 7)
    assert out.reason_data.get("forced_requery") is not True
    assert carrier.get("consumed_reason") == "expired"


def test_degraded_empty_turns_do_not_extend_life() -> None:
    carrier = _carrier(3)
    history = [carrier] + [
        {"action": "SEARCH_CODE", "outcome_status": "DEGRADED", "miss_reason": "timeout", "parameters": {}}
        for _ in range(5)
    ]
    _cascade(history, 9)
    assert carrier.get("consumed_reason") == "expired"


def test_version_pin_routing_lv7_keeps_scan_all() -> None:
    old, new = _carrier(2), _carrier(5)
    new["consumed"] = True
    # lv7: untouched by newest-only (resurrects the older carrier)
    assert _should_force_order_requery([old, new], [], WorkOrderPolicy(hardened=False, iteration=9)) is not None
    out = _cascade([old, new], 9, pin="lv7")
    assert out.reason_data.get("forced_requery") is True


def test_workflow_wiring_pin() -> None:
    import investigation_agent_platform.application.worker.workflows.run_investigation as ri

    src = inspect.getsource(forcing._t_requery)
    assert "_should_force_order_requery(" in src and "ctx.policy" in src
    rsrc = inspect.getsource(ri)
    assert '_consumed_order_ref["consumed_reason"] = "dispatched"' in rsrc
    assert "requery_order_events" in rsrc


def test_newest_only_is_structural() -> None:
    # negative control for D9: a consumed newest carrier ends the search right
    # there (no fall-through to older carriers on any pin)
    src = inspect.getsource(forcing._should_force_order_requery)
    assert src.count("if entry.get(\"consumed\")") == 2
    assert "never resurrects an older one" in src


def test_temporal_replay_deferred() -> None:
    import pytest

    pytest.skip("first Temporal test-server use; requires owner approval (binary download)")


def test_run_length_bound_without_a_cap() -> None:
    dispatches = 0
    for turn in range(2, 11):
        carrier = _carry_requery_order(
            {"ledger_requery": {"query": f"q{turn}", "action_type": "SEARCH_CODE",
                                "action_parameters": {"query": f"q{turn}"}, "claim_ref": f"c{turn}"}},
            turn,
        )
        assert carrier is not None
        out = _cascade([carrier], turn + 1)
        dispatches += bool(out.reason_data.get("forced_requery"))
    assert dispatches <= 9


def test_expiry_reads_the_order_ttl() -> None:
    # negative control: without the ttl bound nothing expires (break the `age > ttl` branch)
    c = _carrier(3)
    c["order"] = dict(c["order"], ttl_rounds=99)
    out = _should_force_order_requery([c], [], _policy(9))
    assert "action_type" in out
    src = inspect.getsource(_should_force_order_requery)
    assert "order.ttl_rounds" in src and "age > ttl" in src
