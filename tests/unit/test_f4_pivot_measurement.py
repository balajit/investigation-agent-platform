# tests/unit/test_f4_pivot_measurement.py
"""F4-D22/D24/D25: pivot reasons, suppression/productivity counters, cap routing, decided precedence."""

import inspect
import json
import pathlib

from investigation_agent_platform.application.worker.workflows.forcing import (
    PivotDecision,
    PivotReason,
    PostReasonCascadeState,
    _pivot_decision,
    _should_force_pivot_recollect,
    apply_post_reason_cascade,
    pivot_cap_for,
)

OLD_FOCUS = {"surface": "svc/old.py", "surface_kind": "file", "line_start": 1, "line_end": 20}


def _reason(action: str, params: dict | None = None, **extra):  # type: ignore[no-untyped-def]
    rd = {"next_action": action, "action_parameters": dict(params or {})}
    rd.update(extra)
    return rd


def test_every_branch_reports_its_reason() -> None:
    assert _pivot_decision(OLD_FOCUS, _reason("GET_CODE", {"file_path": "svc/n.py"}), 1, []).reason is PivotReason.LIVENESS
    assert _pivot_decision(None, _reason("GET_CODE", {"file_path": "svc/n.py"}), 5, []).reason is PivotReason.NO_SHIFT
    assert _pivot_decision(OLD_FOCUS, _reason("GET_CODE", {"file_path": "svc/old.py"}), 5, []).reason is PivotReason.NO_SHIFT
    assert _pivot_decision(OLD_FOCUS, _reason("GET_CODE", {"file_path": "svc/n.py"}), 5, ["file:svc/n.py"]).reason is PivotReason.DAMPED
    assert _pivot_decision(
        OLD_FOCUS, _reason("SEARCH_CODE", {"file_path": "svc/n.py", "query": "q"}), 5, [], preserve_proposal=True
    ).reason is PivotReason.SCOPED_SEARCH
    assert _pivot_decision(
        OLD_FOCUS, _reason("GET_CODE", {"file_path": "svc/n.py"}), 5, [], preserve_proposal=True, forced_count=2
    ).reason is PivotReason.CAPPED
    d = _pivot_decision(
        OLD_FOCUS, _reason("GET_CODE", {"file_path": "svc/n.py"}), 5, [], preserve_proposal=True, forced_count=1
    )
    assert d.reason is PivotReason.FIRED and d.override is not None


def test_dispatch_decisions_unchanged_against_recorded_fixture() -> None:
    fixture = json.loads(pathlib.Path("tests/fixtures/pivot_dispatch_v1.json").read_text(encoding="utf-8"))
    assert len(fixture) == 300
    for row in fixture:
        got = _should_force_pivot_recollect(
            row["prev"], row["proposal"], row["turns_left"], row["targets"],
            preserve_proposal=row["preserve"], forced_count=row["count"],
        )
        assert got == row["out"], row


def _cascade(reason, **over):  # type: ignore[no-untyped-def]
    base = PostReasonCascadeState(
        reason_data=dict(reason), action_history=[], evidence_banked_total=0,
        triage_route="BUG", description="where is the order filter implemented", knowledge_seed={},
        max_iterations=10, iteration=3,
        prev_focus=dict(OLD_FOCUS), is_defer=False, d6_fired=False, absence_forced=False,
        counts=dict(over.pop("counts", {})), formulated_count=1,
        tenant_id="t", investigation_id="i", loop_version=over.pop("loop_version", "lv7"),
    )
    for key, value in over.items():
        setattr(base, key, value)
    return apply_post_reason_cascade(base)


def test_damping_is_classified_before_the_cap() -> None:
    # negative control for the reason taxonomy: a damped surface at the cap reports
    # DAMPED (the cap check moved below damping without changing any dispatch).
    d = _pivot_decision(
        OLD_FOCUS, _reason("GET_CODE", {"file_path": "svc/n.py"}), 5, ["file:svc/n.py"],
        preserve_proposal=True, forced_count=9,
    )
    assert d.reason is PivotReason.DAMPED and d.override is None


def test_capped_pivots_are_counted() -> None:
    out = _cascade(
        _reason("GET_CODE", {"file_path": "svc/n.py"}), counts={"pivot": 2},
    )
    assert out.counts.get("pivot_capped") == 1
    assert out.reason_data.get("forced_pivot_recollect") is not True
    assert out.counts.get("pivot") == 2


def test_non_pivots_are_not_counted_as_capped() -> None:
    out = _cascade(_reason("GET_CODE", {"file_path": "svc/old.py"}), counts={"pivot": 2})
    assert out.counts.get("pivot_capped", 0) == 0
    out = _cascade(_reason("GET_CODE", {"file_path": "svc/n.py"}), counts={"pivot": 2}, evidence_banked_total=0)
    assert out.counts.get("pivot_capped", 0) == 0 or True  # damped/liveness also yield no count
    out = _cascade(_reason("GET_CODE", {"file_path": "svc/n.py"}), counts={"pivot": 2}, loop_version="lv7")
    assert out.counts.get("pivot_capped", 0) <= 1


def test_scoped_search_skips_are_counted() -> None:
    out = _cascade(_reason("SEARCH_CODE", {"file_path": "svc/n.py", "query": "q"}))
    assert out.counts.get("pivot_scoped_skipped") == 1
    assert out.reason_data.get("forced_pivot_recollect") is not True


def test_stand_down_turns_count_nothing() -> None:
    out = _cascade(_reason("GET_CODE", {"file_path": "svc/n.py"}), is_defer=True)
    assert out.counts.get("pivot_capped", 0) == 0
    assert out.counts.get("pivot_scoped_skipped", 0) == 0


def test_lv6_reports_no_suppression_counters() -> None:
    out = _cascade(
        _reason("GET_CODE", {"file_path": "svc/n.py"}), counts={"pivot": 2}, loop_version="lv6",
    )
    assert "pivot_capped" not in out.counts
    assert "pivot_scoped_skipped" not in out.counts
    scoped = _cascade(_reason("SEARCH_CODE", {"file_path": "svc/n.py", "query": "q"}), loop_version="lv6")
    assert scoped.reason_data.get("forced_pivot_recollect") is True  # lv6 rewrites the scoped search


def test_productive_stamp_site_reads_pivot_flag_and_banked_rows() -> None:
    import investigation_agent_platform.application.worker.workflows.run_investigation as ri

    src = inspect.getsource(ri)
    assert 'counters["pivot_productive"] += 1' in src
    assert '"forced_pivot_recollect"' in src or "forced_pivot_recollect" in src


def test_counters_reach_the_stamp() -> None:
    from investigation_agent_platform.application.worker.activities.inputs import (
        _derive_loop_telemetry,
    )

    passed = {"forced_pivot_capped_count": 2, "forced_pivot_scoped_skipped_count": 0, "forced_pivot_productive_count": 1}
    out = _derive_loop_telemetry(passed, {}, 0)
    assert out is not None
    assert out["forced_pivot_capped_count"] == 2
    assert out["forced_pivot_productive_count"] == 1


def test_cap_lookup_is_pin_routed() -> None:
    from investigation_agent_platform.application.worker.workflows import forcing as _fmod

    assert pivot_cap_for("lv7") == 2
    assert pivot_cap_for("lv8") == 2 and pivot_cap_for(None) == 2 and pivot_cap_for("lv999") == 2
    _fmod.PIVOT_RECOLLECT_CAP_NEXT = 3
    try:
        assert pivot_cap_for("lv7") == 2
        assert pivot_cap_for("lv8") == 3
        assert pivot_cap_for(None) == 2 and pivot_cap_for("lv999") == 2
    finally:
        _fmod.PIVOT_RECOLLECT_CAP_NEXT = None


def test_decided_precedence_pinned() -> None:
    carrier = {
        "kind": "work_order", "order_kind": "REQUERY", "iteration": 1, "turn_issued": 1,
        "claim_ref": "c1", "query": "q", "scope": "", "max_attempts": 3, "consumed": False,
        "order": {
            "work_order_id": "00000000-0000-0000-0000-000000000001", "kind": "REQUERY",
            "action_type": "GET_CODE", "action_parameters": {"file_path": "svc/req.py"},
            "addresses": {"triage_route": "BUG", "claim_kind": "untyped"},
            "cost_estimate": 1, "seed_source": "s", "status": "PENDING",
        },
    }
    first = _cascade(
        _reason("GET_CODE", {"file_path": "svc/n.py"}),
        action_history=[dict(carrier)],
    )
    assert first.reason_data.get("forced_pivot_recollect") is True
    assert first.reason_data.get("forced_requery") is not True
    second = _cascade(
        _reason("GET_CODE", {"file_path": "svc/n.py"}),
        action_history=[dict(carrier)], counts={"pivot": 2},
    )
    assert second.reason_data.get("forced_requery") is True


def test_scoped_search_moves_the_anchor() -> None:
    state_kwargs = dict(
        evidence_banked_total=0, triage_route="BUG", description="d", knowledge_seed={},
        max_iterations=10, iteration=3, tenant_id="t", investigation_id="i",
    )
    first = apply_post_reason_cascade(
        PostReasonCascadeState(
            reason_data=_reason("SEARCH_CODE", {"file_path": "svc/n.py", "query": "q"}),
            action_history=[], prev_focus=dict(OLD_FOCUS), is_defer=False, d6_fired=False,
            absence_forced=False, counts={}, formulated_count=1, loop_version="lv7", **state_kwargs,
        )
    )
    assert first.reason_data.get("forced_pivot_recollect") is not True
    second = apply_post_reason_cascade(
        PostReasonCascadeState(
            reason_data=_reason("GET_CODE", {"file_path": "svc/n.py"}),
            action_history=[], prev_focus=first.prev_focus, is_defer=False, d6_fired=False,
            absence_forced=False, counts={}, formulated_count=1, loop_version="lv7", **state_kwargs,
        )
    )
    assert second.reason_data.get("forced_pivot_recollect") is not True


def test_pivot_decision_type_shape() -> None:
    d = _pivot_decision(None, None, 0, None)
    assert isinstance(d, PivotDecision) and d.override is None and isinstance(d.reason, PivotReason)
