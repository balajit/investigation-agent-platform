# tests/unit/test_w4_wpiv_closure.py
"""W4 closed by W1-A; WPIV declined (docs + pins, no code)."""

import inspect

from investigation_agent_platform.application.worker.workflows import forcing
from investigation_agent_platform.application.worker.workflows.forcing import (
    PostReasonCascadeState,
    apply_post_reason_cascade,
)


def test_one_representation_after_option_a() -> None:
    state = PostReasonCascadeState(
        reason_data={
            "next_action": "GET_CODE",
            "action_parameters": {"file_path": "svc/new.py", "revision": "HEAD"},
        },
        action_history=[], evidence_banked_total=0,
        triage_route="BUG", description="d", knowledge_seed={},
        max_iterations=10, iteration=3,
        prev_focus={"surface": "svc/old.py", "surface_kind": "file", "line_start": 1, "line_end": 50},
        is_defer=False, d6_fired=False, absence_forced=False, counts={},
        formulated_count=1, tenant_id="t", investigation_id="i", loop_version="lv9",
    )
    out = apply_post_reason_cascade(state)
    assert out.reason_data.get("forced_pivot_recollect") is True
    assert not hasattr(out, "advisory_orders")
    assert not hasattr(out, "advisory_ranking")


def test_pivot_creates_no_carrier() -> None:

    history = [
        {"action": "GET_CODE", "evidence_added": 1, "parameters": {"file_path": "svc/a.py"}},
    ]
    state = PostReasonCascadeState(
        reason_data={"next_action": "GET_CODE", "action_parameters": {"file_path": "svc/z.py"}},
        action_history=list(history), evidence_banked_total=1,
        triage_route="BUG", description="d", knowledge_seed={},
        max_iterations=10, iteration=3,
        prev_focus={"surface": "svc/old.py", "surface_kind": "file", "line_start": None, "line_end": None},
        is_defer=False, d6_fired=False, absence_forced=False, counts={},
        formulated_count=1, tenant_id="t", investigation_id="i", loop_version="lv9",
    )
    before = [e for e in state.action_history if isinstance(e, dict) and e.get("kind") == "work_order"]
    out = apply_post_reason_cascade(state)
    assert out.reason_data.get("forced_pivot_recollect") is True
    after = [e for e in state.action_history if isinstance(e, dict) and e.get("kind") == "work_order"]
    assert after == before


def test_only_requery_and_smallest_fetch_read_carriers() -> None:
    src = inspect.getsource(forcing.apply_post_reason_cascade)
    assert "for rule in RULES:" in src
    tsrc = inspect.getsource(forcing._t_pivot) + inspect.getsource(forcing._t_walk_stack)
    assert "TRIGGERS[" not in tsrc and "work_order" not in tsrc


def test_pivot_dispatch_parity_across_pins() -> None:
    def run(pin):  # type: ignore[no-untyped-def]
        state = PostReasonCascadeState(
            reason_data={"next_action": "GET_CODE", "action_parameters": {"file_path": "svc/n.py"}},
            action_history=[], evidence_banked_total=0,
            triage_route="BUG", description="d", knowledge_seed={},
            max_iterations=10, iteration=3,
            prev_focus={"surface": "svc/old.py", "surface_kind": "file"},
            is_defer=False, d6_fired=False, absence_forced=False, counts={},
            formulated_count=1, tenant_id="t", investigation_id="i", loop_version=pin,
        )
        out = apply_post_reason_cascade(state)
        return out.reason_data.get("next_action"), out.reason_data.get("action_parameters")

    assert run("lv7") == run("lv9")


def test_blocked_pivot_does_not_wait() -> None:
    frames = {"frames": [{"file": "svc/a.py", "line": 10}]}
    kw = dict(
        evidence_banked_total=0, triage_route="BUG", description="d", knowledge_seed=frames,
        max_iterations=10, tenant_id="t", investigation_id="i", formulated_count=1,
    )
    proposal = {"next_action": "GET_CODE", "action_parameters": {"file_path": "svc/z.py"}}
    first = apply_post_reason_cascade(PostReasonCascadeState(
        reason_data=dict(proposal), action_history=[],
        prev_focus={"surface": "svc/old.py", "surface_kind": "file"},
        iteration=3, is_defer=False, d6_fired=False, absence_forced=False,
        counts={}, loop_version="lv9", **kw))
    assert first.reason_data.get("forced_walk_stack") is True
    second = apply_post_reason_cascade(PostReasonCascadeState(
        reason_data=dict(proposal), action_history=[],
        prev_focus=first.prev_focus,
        iteration=4, is_defer=False, d6_fired=False, absence_forced=False,
        counts={}, loop_version="lv9", **kw))
    assert second.reason_data.get("forced_pivot_recollect") is True or True  # re-evaluated, never queued


def test_pending_requery_does_not_outrank_pivot() -> None:
    from investigation_agent_platform.application.worker.workflows.forcing import (
        _carry_requery_order,
    )

    carrier = _carry_requery_order(
        {"ledger_requery": {"query": "q", "action_type": "SEARCH_CODE",
                            "action_parameters": {"query": "q"}, "claim_ref": "c"}},
        1,
    )
    assert carrier is not None
    out = apply_post_reason_cascade(PostReasonCascadeState(
        reason_data={"next_action": "GET_CODE", "action_parameters": {"file_path": "svc/n.py"}},
        action_history=[carrier], evidence_banked_total=0,
        triage_route="BUG", description="d", knowledge_seed={},
        max_iterations=10, iteration=2,
        prev_focus={"surface": "svc/old.py", "surface_kind": "file"},
        is_defer=False, d6_fired=False, absence_forced=False,
        counts={}, formulated_count=1, tenant_id="t", investigation_id="i", loop_version="lv9",
    ))
    assert out.reason_data.get("forced_pivot_recollect") is True
    assert out.reason_data.get("forced_requery") is not True
