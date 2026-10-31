# tests/unit/test_f8_trigger_loop.py
"""F8-D15/D17: trigger/apply loop behaviors (table-driven cascade)."""



from investigation_agent_platform.application.worker.workflows import forcing
from investigation_agent_platform.application.worker.workflows.forcing import (
    TRIGGERS,
    PostReasonCascadeState,
    apply_post_reason_cascade,
    check_triggers,
)
from investigation_agent_platform.domain.investigation.forcing_rules import Force, Rule


def _state(reason, **over):  # type: ignore[no-untyped-def]
    base = PostReasonCascadeState(
        reason_data=reason,
        action_history=[],
        evidence_banked_total=5,
        triage_route="BUG",
        description="where is the order filter implemented",
        knowledge_seed={},
        max_iterations=10,
        iteration=3,
        prev_focus={"surface": "svc/old.py", "surface_kind": "file", "line_start": None, "line_end": None},
        is_defer=False,
        d6_fired=False,
        absence_forced=False,
        counts={},
        formulated_count=1,
        tenant_id="t",
        investigation_id="i",
        loop_version="lv7",
    )
    for key, value in over.items():
        setattr(base, key, value)
    return base


def test_triggers_match_the_table() -> None:
    assert check_triggers(forcing.RULES, TRIGGERS) == []
    assert set(TRIGGERS) == {r.name for r in forcing.RULES}


def test_rule_without_trigger_is_rejected() -> None:
    bad = [Rule("ghost", Force.REPLAN, frozenset())]
    assert "rule ghost has no trigger" in check_triggers(bad, TRIGGERS)
    assert check_triggers(forcing.RULES, {}) != []


def test_cancels_contract() -> None:
    from investigation_agent_platform.domain.investigation.forcing_rules import check_rule_contract

    bad = Rule("x", Force.WALK_STACK, frozenset(), cancels=frozenset({Force.PIVOT_RECOLLECT}))
    assert any("cancels" in p for p in check_rule_contract([bad]))
    assert check_rule_contract(list(forcing.RULES)) == []
    walk = next(r for r in forcing.RULES if r.name == "walk_stack")
    assert Force.PIVOT_RECOLLECT in walk.cancels and Force.PIVOT_RECOLLECT in walk.exempt


def test_cancel_refunds_only_what_was_counted() -> None:
    frames = {"frames": [{"file": "svc/a.py", "line": 10}]}
    reason = {"next_action": "GET_CODE", "action_parameters": {"file_path": "svc/z.py"}}
    out = apply_post_reason_cascade(_state(reason, knowledge_seed=frames))
    assert out.reason_data.get("forced_walk_stack") is True
    assert out.reason_data.get("forced_pivot_recollect") is False
    assert out.counts.get("pivot", 0) == 0
    # pivot never fired (known surface, no pivot): walk-stack still writes False (unconditional)
    out2 = apply_post_reason_cascade(
        _state(
            {"next_action": "GET_CODE", "action_parameters": {"file_path": "svc/old.py"}},
            knowledge_seed=frames,
        )
    )
    assert out2.reason_data.get("forced_walk_stack") is True
    assert out2.reason_data.get("forced_pivot_recollect") is False
    # lv6: no pivot counter involved
    out3 = apply_post_reason_cascade(
        _state(reason, knowledge_seed=frames, loop_version="lv6")
    )
    assert "pivot" not in out3.counts


def test_requery_effects_only_fire() -> None:
    # budget-exhausted: a later stamp for the same claim/query/scope retires the
    # carrier without dispatch (mark_consumed path).
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
    stamp = {
        "action": "GET_CODE", "evidence_added": 1, "iteration": 2,
        "parameters": {"file_path": "svc/req.py"},
        "requery_consumed": {"claim_ref": "c1", "query": "q", "scope": ""},
    }
    out = apply_post_reason_cascade(
        _state({"next_action": "FORMULATE_HYPOTHESIS"}, action_history=[carrier, stamp], evidence_banked_total=1)
    )
    assert out.reason_data.get("forced_requery") is not True
    assert carrier["consumed"] is True
    assert out.counts.get("requery", 0) == 0


def test_trigger_fault_passes_proposal_through_uncommitted(caplog) -> None:  # type: ignore[no-untyped-def]
    import logging

    orig = TRIGGERS
    calls: list = []

    def boom(ctx):  # type: ignore[no-untyped-def]
        calls.append(1)
        raise RuntimeError("trigger boom")

    forcing.TRIGGERS = dict(TRIGGERS, replan=boom)
    try:
        # known surface: no pivot, no earlier force — replan is evaluated, then raises
        reason = {"next_action": "GET_CODE", "action_parameters": {"file_path": "svc/old.py"}}
        with caplog.at_level(logging.WARNING):
            out = apply_post_reason_cascade(_state(reason))
        assert out.reason_data == reason
        assert out.counts == {}
        assert calls == [1]
    finally:
        forcing.TRIGGERS = orig


def test_smallest_fetch_counters_are_per_kind() -> None:
    from investigation_agent_platform.application.worker.workflows.forcing import _carry_gap_orders

    carrier = _carry_gap_orders(
        {"claim_gaps": [
            {"gap_id": "gh", "kind": "HISTORY", "target": {"file_path": "svc/h.py"}},
            {"gap_id": "gb", "kind": "BEHAVIOR", "target": {"file_path": "svc/b.py"}},
        ]},
        2,
    )
    assert carrier is not None
    out = apply_post_reason_cascade(
        _state({"next_action": "FORMULATE_HYPOTHESIS"}, action_history=[carrier], evidence_banked_total=9)
    )
    assert out.reason_data.get("forced_smallest_fetch") is True
    assert out.counts.get("smallest_fetch_history") == 1
    assert out.counts.get("smallest_fetch_causal", 0) == 0


def test_yielding_rules_never_evaluate_their_trigger() -> None:
    seen: list = []
    wrapped = {}
    for name, fn in TRIGGERS.items():
        def make(n, f):  # type: ignore[no-untyped-def]
            def spy(ctx):  # type: ignore[no-untyped-def]
                seen.append(n)
                return f(ctx)
            return spy
        wrapped[name] = make(name, fn)
    forcing.TRIGGERS = wrapped
    try:
        apply_post_reason_cascade(_state({"next_action": "GET_CODE", "action_parameters": {}}, is_defer=True))
        assert seen == []
        seen.clear()
        apply_post_reason_cascade(
            _state(
                {"next_action": "GET_CODE", "action_parameters": {}, "forced_not_checkable": True},
                d6_fired=True,
            )
        )
        assert seen == []
    finally:
        forcing.TRIGGERS = TRIGGERS


def test_new_rule_needs_no_cascade_edit(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.worker.workflows.forcing import Fire

    def probe_trigger(ctx):  # type: ignore[no-untyped-def]
        return Fire("SEARCH_LOGS", {"query": "probe"})

    probe = Rule("probe", Force.REPLAN, frozenset({f.value for f in Force if f != Force.REPLAN}))
    monkeypatch.setattr(forcing, "RULES", tuple(forcing.RULES) + (probe,))
    monkeypatch.setattr(forcing, "TRIGGERS", dict(TRIGGERS, probe=probe_trigger))
    monkeypatch.setattr(forcing, "_RULES_BY_NAME", {**forcing._RULES_BY_NAME, "probe": probe})
    out = apply_post_reason_cascade(
        _state({"next_action": "GET_CODE", "action_parameters": {"file_path": "svc/old.py"}})
    )
    assert out.reason_data["next_action"] == "SEARCH_LOGS"
