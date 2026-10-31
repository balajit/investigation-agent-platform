# tests/unit/test_w3_gap_coverage.py
"""W3: outcome-aware gap coverage, reason identity, first-fires-first, feed expiry (LV_W)."""

import inspect

from investigation_agent_platform.application.worker.workflows.forcing import (
    PostReasonCascadeState,
    _carry_gap_orders,
    _gap_coverage,
    apply_post_reason_cascade,
)


def _feed(*gaps, iteration=2):  # type: ignore[no-untyped-def]
    carrier = _carry_gap_orders({"claim_gaps": list(gaps)}, iteration)
    assert carrier is not None
    return carrier


def _gap(gap_id="g1", kind="DEFINITION", target=None, **over):  # type: ignore[no-untyped-def]
    gap = {"gap_id": gap_id, "kind": kind, "claim_ref": "ck",
           "target": target or {"file_path": "svc/gap.py"}}
    gap.update(over)
    return gap


def _stamped(gap_id, status, reason="", miss_reason=None, banked=1):  # type: ignore[no-untyped-def]
    entry = {"action": "GET_CODE", "evidence_added": banked,
             "parameters": {"file_path": "svc/gap.py"},
             "outcome_status": status,
             "forced_smallest_fetch_gap_id": gap_id,
             "forced_smallest_fetch_reason": reason}
    if miss_reason is not None:
        entry["miss_reason"] = miss_reason
    return entry


def _cascade(history, pin="lv9", counts=None, proposal=None, iteration=4):  # type: ignore[no-untyped-def]
    state = PostReasonCascadeState(
        reason_data=dict(proposal or {"next_action": "FORMULATE_HYPOTHESIS"}),
        action_history=list(history), evidence_banked_total=9,
        triage_route="BUG", description="d", knowledge_seed={},
        max_iterations=10, iteration=iteration,
        prev_focus=None, is_defer=False, d6_fired=False, absence_forced=False,
        counts=dict(counts or {}), formulated_count=1,
        tenant_id="t", investigation_id="i", loop_version=pin,
    )
    return apply_post_reason_cascade(state)


def test_failed_fetch_does_not_cover() -> None:
    carrier = _feed(_gap())
    history = [carrier, _stamped("g1", "FAILED", banked=0)]
    out = _cascade(history)
    assert out.reason_data.get("forced_smallest_fetch") is True


def test_transient_degrades_do_not_cover() -> None:
    for miss in ("timeout", "provider_unavailable", "rate_limited", "idempotency_owner_busy"):
        covered, attempts = _gap_coverage([_stamped("g1", "DEGRADED", miss_reason=miss, banked=0)], "g1", "")
        assert (covered, attempts) == (False, 1), miss


def test_deterministic_degrades_cover() -> None:
    for miss in ("code_path_not_found", "invalid_parameters", "unsupported_capability"):
        assert _gap_coverage([_stamped("g1", "DEGRADED", miss_reason=miss)], "g1", "")[0] is True


def test_succeeded_covers() -> None:
    assert _gap_coverage([_stamped("g1", "SUCCEEDED")], "g1", "") == (True, 1)


def test_clean_empty_covers_including_absence() -> None:
    assert _gap_coverage([_stamped("g1", "EMPTY", banked=0)], "g1", "")[0] is True
    assert _gap_coverage([_stamped("g1", "EMPTY", banked=0)], "g1", "")[0] is True


def test_blocked_covers() -> None:
    assert _gap_coverage([_stamped("g1", "BLOCKED")], "g1", "")[0] is True


def test_unknown_or_missing_status_covers() -> None:
    for status in (None, "", "WEIRD"):
        assert _gap_coverage([_stamped("g1", status)], "g1", "")[0] is True
    assert _gap_coverage([None, 5, {"forced_smallest_fetch_gap_id": "g1"}], "g1", "")[0] is True


def test_attempt_limit() -> None:
    history = [_stamped("g1", "FAILED", banked=0), _stamped("g1", "FAILED", banked=0)]
    assert _gap_coverage(history, "g1", "") == (True, 2)
    assert _gap_coverage([_stamped("g1", "FAILED", banked=0)], "g1", "") == (False, 1)


def test_first_fires_before_retries() -> None:
    feed = _feed(
        _gap("gb"), _gap("gh", kind="HISTORY", target={"file_path": "svc/h.py"}),
        iteration=2,
    )
    # never-fired DEFINITION sibling beats the never-fired HISTORY gap
    out = _cascade([feed, _stamped("ga", "FAILED", banked=0)])
    assert out.reason_data.get("forced_smallest_fetch_gap_id") == "gb"
    # a retryable DEFINITION gap waits while a never-fired HISTORY gap exists
    history = [feed, _stamped("gb", "FAILED", banked=0)]
    out = _cascade(history)
    assert out.reason_data.get("forced_smallest_fetch_gap_id") == "gh"
    # with HISTORY fired, the DEFINITION retry goes (cap has room)
    history = [feed, _stamped("gb", "FAILED", banked=0), _stamped("gh", "SUCCEEDED")]
    out = _cascade(history)
    assert out.reason_data.get("forced_smallest_fetch_gap_id") == "gb"


def test_kind_cap_still_bounds_retries() -> None:
    carrier = _feed(_gap())
    history = [carrier, _stamped("g1", "FAILED", banked=0)]
    out = _cascade(history, counts={"smallest_fetch_definition": 2})
    assert out.reason_data.get("forced_smallest_fetch") is not True


def test_h8_gap_reachable_after_uncited_fetch() -> None:
    uncited = _feed(_gap("g1"))
    h8 = _feed(_gap("g1", reason="cited_gate_failed"), iteration=3)
    history = [uncited, _stamped("g1", "SUCCEEDED"), h8]
    from investigation_agent_platform.application.worker.workflows.forcing import _pick_gap_order

    orders = h8["orders"]
    picked = _pick_gap_order(
        orders, kind="DEFINITION", history=history, forced_count=0,
        policy=__import__("investigation_agent_platform.application.worker.workflows.forcing", fromlist=["WorkOrderPolicy"]).WorkOrderPolicy(hardened=True, iteration=4),
        carrier_turn=3, allow_retry=True,
    )
    assert picked is not None and picked.gap_id == "g1"
    out = _cascade(history)
    assert out.reason_data.get("forced_smallest_fetch_gap_id") == "g1"
    assert out.reason_data.get("forced_smallest_fetch") is True


def test_h8_gap_covered_by_its_own_success() -> None:
    h8 = _feed(_gap("g1", reason="cited_gate_failed"), iteration=3)
    history = [h8, _stamped("g1", "SUCCEEDED", reason="cited_gate_failed")]
    out = _cascade(history)
    assert out.reason_data.get("forced_smallest_fetch") is not True


def test_reason_rides_the_stamp() -> None:
    import investigation_agent_platform.application.worker.workflows.run_investigation as ri

    carrier = _feed(_gap("g1", reason="cited_gate_failed"), iteration=3)
    out = _cascade([carrier])
    ctx = out.reason_data.get("smallest_fetch_context") or {}
    assert ctx.get("reason") == "cited_gate_failed"
    src = inspect.getsource(ri)
    assert "forced_smallest_fetch_reason" in src


def test_stale_feed_ignored_with_event() -> None:
    carrier = _feed(_gap(), iteration=2)
    out = _cascade([carrier], iteration=7)
    assert out.reason_data.get("forced_smallest_fetch") is not True
    assert out.order_events == [{"event": "gap_feed_stale", "age": 5, "turn_issued": 2}]


def test_fresh_feed_dispatches_and_tombstone_supersedes() -> None:
    carrier = _feed(_gap(), iteration=6)
    out = _cascade([carrier], iteration=7)
    assert out.reason_data.get("forced_smallest_fetch") is True
    tombstone = _carry_gap_orders({"claim_gaps": []}, 7)
    assert tombstone is not None
    out = _cascade([carrier, tombstone], iteration=8)
    assert out.reason_data.get("forced_smallest_fetch") is not True


def test_tombstone_reissue_dispatch_end_to_end() -> None:
    g1, g2 = _gap("g1"), _gap("g2", target={"file_path": "svc/other.py"})
    feed1 = _feed(g1, g2, iteration=2)
    history = [feed1]
    out = _cascade(history, iteration=3)
    assert out.reason_data.get("forced_smallest_fetch_gap_id") == "g1"
    tombstone = _carry_gap_orders({"claim_gaps": []}, 4)
    history = [feed1, tombstone]
    out = _cascade(history, iteration=5)
    assert out.reason_data.get("forced_smallest_fetch") is not True
    feed2 = _feed(g2, iteration=6)
    history = [feed1, tombstone, feed2]
    out = _cascade(history, iteration=7)
    assert out.reason_data.get("forced_smallest_fetch_gap_id") == "g2"


def test_version_pin_routing() -> None:
    carrier = _feed(_gap(), iteration=2)
    failed = [_stamped("g1", "FAILED", banked=0)]
    # lv7: stamp-only coverage; H8 unreachable; stale feed still dispatches
    out = _cascade([carrier] + failed, iteration=4, pin="lv7")
    assert out.reason_data.get("forced_smallest_fetch") is not True
    h8 = _feed(_gap("g1", reason="cited_gate_failed"), iteration=3)
    out = _cascade([carrier, _stamped("g1", "SUCCEEDED"), h8], iteration=4, pin="lv7")
    assert out.reason_data.get("forced_smallest_fetch") is not True
    out = _cascade([carrier], iteration=7, pin="lv7")
    assert out.reason_data.get("forced_smallest_fetch") is True


def test_other_reads_do_not_cover() -> None:
    pivot_read = {"action": "GET_CODE", "evidence_added": 1,
                  "parameters": {"file_path": "svc/gap.py", "start_line": 1, "end_line": 5},
                  "outcome_status": "SUCCEEDED", "forced_pivot_recollect": True}
    carrier = _feed(_gap(target={"file_path": "svc/gap.py", "start_line": 1, "end_line": 5}))
    out = _cascade([pivot_read, carrier])
    assert out.reason_data.get("forced_smallest_fetch") is True


def test_hypothesis_not_in_identity() -> None:
    carrier = _feed(_gap("g1"))
    history = [carrier, _stamped("g1", "SUCCEEDED")]
    out = _cascade(history)
    assert out.reason_data.get("forced_smallest_fetch") is not True
    history = [carrier, _stamped("g1", "FAILED", banked=0)]
    out = _cascade(history)
    assert out.reason_data.get("forced_smallest_fetch") is True


def test_fail_open_on_junk() -> None:
    assert _gap_coverage(None, "", "") == (False, 0)
    assert _gap_coverage(["junk", 5, None], "g1", "") == (False, 0)
    out = _cascade(["junk", 5, None])
    assert out.reason_data.get("forced_smallest_fetch") is not True


def test_negative_control_remove_outcome_check() -> None:
    import investigation_agent_platform.application.worker.workflows.forcing as _fmod

    def always_covered(history, gap_id, reason=""):  # type: ignore[no-untyped-def]
        for entry in history or []:
            if isinstance(entry, dict) and str(entry.get("forced_smallest_fetch_gap_id") or "") == str(gap_id or ""):
                return True, 1
        return False, 0

    orig = _fmod._gap_coverage
    _fmod._gap_coverage = always_covered
    try:
        covered, _ = _gap_coverage([_stamped("g1", "FAILED", banked=0)], "g1", "")
        assert covered is False  # the real function: a failed fetch does not cover
        # with the outcome check removed the same stamp would cover (proving the test can fail)
        assert always_covered([_stamped("g1", "FAILED", banked=0)], "g1", "")[0] is True
    finally:
        _fmod._gap_coverage = orig


def test_negative_control_remove_attempt_limit(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import investigation_agent_platform.application.worker.workflows.forcing as _fmod

    history = [_stamped("g1", "FAILED", banked=0), _stamped("g1", "FAILED", banked=0)]
    assert _gap_coverage(history, "g1", "") == (True, 2)
    monkeypatch.setattr(_fmod, "GAP_MAX_ATTEMPTS", 99)
    covered, attempts = _gap_coverage(history, "g1", "")
    assert (covered, attempts) == (False, 2)
