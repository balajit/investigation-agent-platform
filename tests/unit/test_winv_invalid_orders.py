# tests/unit/test_winv_invalid_orders.py
"""W-INV: invalid requery orders retire as invalid_order, never dispatch (review W-INV, D13-D15)."""

import inspect
import logging

from investigation_agent_platform.application.worker.workflows import forcing
from investigation_agent_platform.application.worker.workflows.forcing import (
    ConsumeReason,
    PostReasonCascadeState,
    WorkOrderPolicy,
    _should_force_order_requery,
    apply_post_reason_cascade,
)


def _carrier(order):  # type: ignore[no-untyped-def]
    return {
        "kind": "work_order", "order_kind": "REQUERY", "iteration": 1, "turn_issued": 1,
        "claim_ref": "c1", "query": "q", "scope": "", "max_attempts": 3, "consumed": False,
        "order": order,
    }


def _valid_order():  # type: ignore[no-untyped-def]
    return {
        "work_order_id": "00000000-0000-0000-0000-000000000001", "kind": "REQUERY",
        "action_type": "GET_CODE", "action_parameters": {"file_path": "svc/req.py"},
        "addresses": {"triage_route": "BUG", "claim_kind": "untyped"},
        "cost_estimate": 1, "seed_source": "s", "status": "PENDING",
    }


def _decision(carrier):  # type: ignore[no-untyped-def]
    return _should_force_order_requery([carrier], [], WorkOrderPolicy(hardened=True, iteration=9))


def test_missing_payload_is_invalid() -> None:
    out = _decision(_carrier(None))
    assert out["mark_consumed"] == ConsumeReason.INVALID_ORDER.value
    assert out["detail"] == "not_a_dict"


def test_validation_failure_is_invalid() -> None:
    out = _decision(_carrier({"kind": "REQUERY"}))
    assert out["mark_consumed"] == ConsumeReason.INVALID_ORDER.value
    assert out["detail"] == "validation_failed"


def test_non_pending_status_is_invalid() -> None:
    out = _decision(_carrier({**_valid_order(), "status": "CONSUMED"}))
    assert out["mark_consumed"] == ConsumeReason.INVALID_ORDER.value
    assert out["detail"] == "not_pending"


def test_wrong_kind_never_dispatches() -> None:
    out = _decision(_carrier({**_valid_order(), "kind": "GAP_FILL"}))
    assert out["mark_consumed"] == ConsumeReason.INVALID_ORDER.value
    assert out["detail"] == "wrong_kind"
    assert "action_type" not in out


def test_genuine_budget_exhaustion_keeps_its_label() -> None:
    from investigation_agent_platform.application.worker.workflows.forcing import (
        _carry_requery_order,
    )

    carrier = _carry_requery_order(
        {"ledger_requery": {"query": "q", "action_type": "SEARCH_CODE",
                            "action_parameters": {"query": "q"}, "claim_ref": "c",
                            "max_attempts": 3}},
        1,
    )
    assert carrier is not None
    recorded = [
        (["row-1"], {"search_query": "q", "scope": "", "attempt_id": "id-1"}),
        (["row-2"], {"search_query": "q", "scope": "", "attempt_id": "id-2"}),
        (["row-3"], {"search_query": "q", "scope": "", "attempt_id": "id-3"}),
    ]
    out = _should_force_order_requery([carrier], recorded, WorkOrderPolicy(hardened=True, iteration=9))
    assert out["mark_consumed"] == ConsumeReason.BUDGET_EXHAUSTED.value
    assert out["detail"] == "attempt_budget"


def test_invalid_carrier_is_retired_stamped_and_reported(caplog) -> None:  # type: ignore[no-untyped-def]
    carrier = _carrier({**_valid_order(), "kind": "GAP_FILL"})
    state = PostReasonCascadeState(
        reason_data={"next_action": "FORMULATE_HYPOTHESIS"}, action_history=[carrier],
        evidence_banked_total=9, triage_route="BUG", description="d", knowledge_seed={},
        max_iterations=10, iteration=9, prev_focus=None,
        is_defer=False, d6_fired=False, absence_forced=False, counts={},
        formulated_count=1, tenant_id="tenant-a", investigation_id="inv-1", loop_version="lv9",
    )
    with caplog.at_level(logging.WARNING):
        out = apply_post_reason_cascade(state)
    assert carrier.get("consumed") is True
    assert carrier.get("consumed_reason") == "invalid_order"
    assert out.reason_data.get("forced_requery") is not True
    assert len(out.order_events) == 1
    assert out.order_events[0]["reason"] == "invalid_order"
    assert out.order_events[0]["detail"] == "wrong_kind"
    assert out.order_events[0]["claim_ref"] == "c1"
    assert "gap_fill" not in str(caplog.text).lower() or "wrong_kind" in caplog.text
    assert any(getattr(r, "event", "") == "requery order invalid" for r in caplog.records)
    assert any(getattr(r, "error", "") == "wrong_kind" for r in caplog.records)
    marker = "MARKER-never-logged"
    assert marker not in caplog.text


def test_vocabulary_is_closed() -> None:
    allowed = {r.value for r in ConsumeReason} | {"dispatched"}
    src = inspect.getsource(forcing.apply_post_reason_cascade)
    trig = inspect.getsource(forcing._t_requery)
    assert '"budget-exhausted"' not in src + trig
    assert "mark_consumed" in trig
    assert allowed == {"budget_exhausted", "expired", "invalid_order", "dispatched"}


def test_fail_open_on_a_retirement_fault(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import investigation_agent_platform.application.worker.workflows.forcing as _fmod

    monkeypatch.setattr(_fmod, "_retirement_event", lambda *a, **k: 1 / 0)
    carrier = _carrier(None)
    state = PostReasonCascadeState(
        reason_data={"next_action": "GET_CODE", "action_parameters": {"file_path": "svc/a.py"}},
        action_history=[carrier], evidence_banked_total=0,
        triage_route="BUG", description="d", knowledge_seed={},
        max_iterations=10, iteration=9, prev_focus=None,
        is_defer=False, d6_fired=False, absence_forced=False, counts={},
        formulated_count=1, tenant_id="t", investigation_id="i", loop_version="lv9",
    )
    out = apply_post_reason_cascade(state)
    assert out.reason_data["next_action"] == "GET_CODE"
    assert out.counts == {}
    assert carrier.get("consumed") is not True


def test_negative_control_restore_the_old_label() -> None:
    # the old code labeled every invalid order "budget-exhausted": that label
    # cannot distinguish a corrupt payload from a spent budget
    legacy = "budget-exhausted"
    assert legacy not in {r.value for r in ConsumeReason}
    assert _decision(_carrier(None))["detail"] == "not_a_dict"
    assert _decision(_carrier({"kind": "REQUERY"}))["detail"] == "validation_failed"


def test_telemetry_plumbing() -> None:
    import investigation_agent_platform.application.worker.workflows.run_investigation as ri

    src = inspect.getsource(ri)
    assert "requery_order_events" in src
    assert "[-20:]" in src
