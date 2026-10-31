# tests/unit/test_w5_focus_semantics.py
"""W5: canonical file identity, sanitized windows, contract repairs (LV_W, D23-D28)."""

import inspect
import json

from investigation_agent_platform.application.worker.workflows import forcing
from investigation_agent_platform.application.worker.workflows.forcing import (
    PostReasonCascadeState,
    apply_post_reason_cascade,
)
from investigation_agent_platform.domain.collection.focus import (
    FocusPoint,
    FocusShift,
    canonical_surface,
    detect_shift,
    focus_from_reason_data,
    pivot_can_pay_off,
)


def _focus(surface, kind="file", start=None, end=None):  # type: ignore[no-untyped-def]
    return FocusPoint(surface=surface, surface_kind=kind, line_start=start, line_end=end)


def _reason(action, params):  # type: ignore[no-untyped-def]
    return {"next_action": action, "action_parameters": dict(params)}


def test_same_file_different_spelling_is_not_a_pivot() -> None:
    prev = _focus("src/A.java")
    for spelling in ("./src/A.java", "src\\A.java", "src//A.java", "src/./A.java", "src/A.java/", "  src/A.java "):
        curr = focus_from_reason_data(_reason("GET_CODE", {"file_path": spelling}), hardened=True)
        assert curr is not None
        assert detect_shift(prev, curr, hardened=True) is FocusShift.NONE, spelling


def test_case_is_preserved() -> None:
    prev = _focus("src/A.java")
    curr = focus_from_reason_data(_reason("GET_CODE", {"file_path": "src/a.java"}), hardened=True)
    assert detect_shift(prev, curr, hardened=True) is FocusShift.PIVOTED


def test_absolute_stays_distinct() -> None:
    prev = _focus("src/A.java")
    curr = focus_from_reason_data(_reason("GET_CODE", {"file_path": "/repo/src/A.java"}), hardened=True)
    assert detect_shift(prev, curr, hardened=True) is FocusShift.PIVOTED


def test_version_pin_routing_lv7_keeps_raw_identity() -> None:
    proposal = _reason("GET_CODE", {"file_path": "./src/A.java"})
    prev = {"surface": "src/A.java", "surface_kind": "file"}
    state_kw = dict(
        evidence_banked_total=0, triage_route="BUG", description="d", knowledge_seed={},
        max_iterations=10, iteration=3, tenant_id="t", investigation_id="i", formulated_count=1,
    )
    old = apply_post_reason_cascade(PostReasonCascadeState(
        reason_data=dict(proposal), action_history=[], prev_focus=dict(prev),
        is_defer=False, d6_fired=False, absence_forced=False, counts={},
        loop_version="lv7", **state_kw,
    ))
    assert old.reason_data.get("forced_pivot_recollect") is True
    new = apply_post_reason_cascade(PostReasonCascadeState(
        reason_data=dict(proposal), action_history=[], prev_focus=dict(prev),
        is_defer=False, d6_fired=False, absence_forced=False, counts={},
        loop_version="lv9", **state_kw,
    ))
    assert new.reason_data.get("forced_pivot_recollect") is not True


def _should_force_pivot_recover(proposal):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.worker.workflows.forcing import (
        _should_force_pivot_recollect,
    )

    return _should_force_pivot_recollect(
        {"surface": "svc/old.py", "surface_kind": "file"}, proposal, 5,
        ["file:./src/A.java"], preserve_proposal=True, forced_count=0, hardened=True,
    )


def test_damping_compares_canonical_spellings() -> None:
    proposal = _reason("GET_CODE", {"file_path": "src/A.java"})
    assert _should_force_pivot_recover(proposal) is None
    proposal2 = _reason("GET_CODE", {"file_path": "src/B.java"})
    assert _should_force_pivot_recover(proposal2) is not None


def test_dispatch_keeps_model_spelling() -> None:
    proposal = _reason("GET_CODE", {"file_path": "./src/B.java", "revision": "abc"})
    state = PostReasonCascadeState(
        reason_data=dict(proposal), action_history=[],
        prev_focus={"surface": "src/old.py", "surface_kind": "file"},
        evidence_banked_total=0, triage_route="BUG", description="d", knowledge_seed={},
        max_iterations=10, iteration=3, is_defer=False, d6_fired=False,
        absence_forced=False, counts={}, formulated_count=1,
        tenant_id="t", investigation_id="i", loop_version="lv9",
    )
    out = apply_post_reason_cascade(state)
    assert out.reason_data.get("forced_pivot_recollect") is True
    assert out.reason_data["action_parameters"]["file_path"] == "./src/B.java"


def test_infinite_line_keeps_focus() -> None:
    curr = focus_from_reason_data(_reason("GET_CODE", {"file_path": "svc/a.py", "start_line": float("inf")}))
    assert curr is not None and curr.line_start is None
    curr = focus_from_reason_data(_reason("GET_CODE", {"file_path": "svc/a.py", "start_line": "1e999"}))
    assert curr is not None and curr.line_start is None


def test_payoff_never_raises() -> None:
    for bad in (float("inf"), float("-inf"), float("nan"), True, None, [1], {}, ""):
        assert pivot_can_pay_off(bad) is False
    assert pivot_can_pay_off(2) is True
    assert pivot_can_pay_off("3") is True  # numeric strings convert like before


def test_nan_and_non_integers_drop_only_the_line() -> None:
    for bad in (float("nan"), "x", 1.5e400, {"n": 1}):
        curr = focus_from_reason_data(_reason("GET_CODE", {"file_path": "svc/a.py", "start_line": bad}))
        assert curr is not None and curr.surface == "svc/a.py" and curr.line_start is None, bad


def test_oversized_line_absent_under_hardened() -> None:
    curr = focus_from_reason_data(
        _reason("GET_CODE", {"file_path": "svc/a.py", "start_line": 10**30}), hardened=True)
    assert curr is not None and curr.line_start is None
    curr = focus_from_reason_data(
        _reason("GET_CODE", {"file_path": "svc/a.py", "start_line": 10**30}), hardened=False)
    assert curr is not None and curr.line_start == 10**30


def test_inverted_window_drops_end_under_hardened() -> None:
    curr = focus_from_reason_data(
        _reason("GET_CODE", {"file_path": "svc/a.py", "start_line": 50, "end_line": 10}), hardened=True)
    assert curr is not None and (curr.line_start, curr.line_end) == (50, None)
    curr = focus_from_reason_data(
        _reason("GET_CODE", {"file_path": "svc/a.py", "start_line": 50, "end_line": 10}), hardened=False)
    assert curr is not None and (curr.line_start, curr.line_end) == (50, 10)


def test_inverted_window_not_spurious_refinement() -> None:
    prev = _focus("svc/a.py", start=50, end=None)
    curr = focus_from_reason_data(
        _reason("GET_CODE", {"file_path": "svc/a.py", "start_line": 50, "end_line": 10}),
        hardened=True,
    )
    assert curr is not None and (curr.line_start, curr.line_end) == (50, None)
    assert detect_shift(prev, curr, hardened=True) is FocusShift.NONE


def test_canonical_surface_is_total() -> None:
    for junk in (None, "", ".", "\x00", "x" * 1_000_000, b"bytes", object()):
        assert isinstance(canonical_surface(junk), str)
    assert canonical_surface("") == "" and canonical_surface(".") == ""
    assert canonical_surface("./src/A.java") == "src/A.java"


def test_symbol_surface_not_path_normalized() -> None:
    prev = _focus("./Foo", kind="symbol")
    curr = _focus("Foo", kind="symbol")
    assert detect_shift(prev, curr, hardened=True) is FocusShift.PIVOTED


def test_anchor_survives_non_focus_turn() -> None:
    kw = dict(
        evidence_banked_total=0, triage_route="BUG", description="d", knowledge_seed={},
        max_iterations=10, tenant_id="t", investigation_id="i", formulated_count=1,
    )
    s1 = PostReasonCascadeState(
        reason_data=_reason("GET_CODE", {"file_path": "svc/a.py"}), action_history=[],
        prev_focus=None, iteration=1, is_defer=False, d6_fired=False, absence_forced=False,
        counts={}, loop_version="lv9", **kw)
    o1 = apply_post_reason_cascade(s1)
    s2 = PostReasonCascadeState(
        reason_data={"next_action": "FORMULATE_HYPOTHESIS"}, action_history=[],
        prev_focus=o1.prev_focus, iteration=2, is_defer=False, d6_fired=False,
        absence_forced=False, counts={}, loop_version="lv9", **kw)
    o2 = apply_post_reason_cascade(s2)
    s3 = PostReasonCascadeState(
        reason_data=_reason("GET_CODE", {"file_path": "svc/b.py"}), action_history=[],
        prev_focus=o2.prev_focus, iteration=3, is_defer=False, d6_fired=False,
        absence_forced=False, counts={}, loop_version="lv9", **kw)
    o3 = apply_post_reason_cascade(s3)
    assert o3.reason_data.get("forced_pivot_recollect") is True


def test_anchor_is_replay_safe_json() -> None:
    out = apply_post_reason_cascade(PostReasonCascadeState(
        reason_data=_reason("GET_CODE", {"file_path": "svc/a.py"}), action_history=[],
        prev_focus=None, evidence_banked_total=0, triage_route="BUG", description="d",
        knowledge_seed={}, max_iterations=10, iteration=1, is_defer=False, d6_fired=False,
        absence_forced=False, counts={}, formulated_count=1,
        tenant_id="t", investigation_id="i", loop_version="lv9"))
    assert json.loads(json.dumps(out.prev_focus)) == out.prev_focus


def test_wiring_hardened_reaches_focus_code() -> None:
    src = inspect.getsource(forcing.apply_post_reason_cascade)
    assert "hardened=_wo_policy.hardened" in src or "hardened=ctx.policy.hardened" in src
    tsrc = inspect.getsource(forcing._t_pivot)
    assert "hardened=" in tsrc
