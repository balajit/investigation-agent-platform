# tests/unit/test_cascade_seam.py
"""2b seam: the real post-reason cascade under controlled inputs.

Drives forcing.apply_post_reason_cascade with crafted reason outputs
and histories — no services, no model calls. Pins final dispatched
actions for competing rules, symbol damping, kind-qualified surfaces,
and deterministic multi-turn focus state.
"""

from investigation_agent_platform.application.worker.workflows.forcing import (
    PostReasonCascadeState,
    apply_post_reason_cascade,
)


def _state(**over):  # type: ignore[no-untyped-def]
    base = PostReasonCascadeState(
        reason_data={
            "next_action": "GET_CODE",
            "action_parameters": {
                "file_path": "svc/new.py",
                "start_line": 1,
                "end_line": 20,
            },
        },
        action_history=[],
        evidence_banked_total=5,
        triage_route="BUG",
        description="where is the order filter implemented",
        knowledge_seed={},
        max_iterations=10,
        iteration=3,
        prev_focus={
            "surface": "svc/old.py",
            "surface_kind": "file",
            "line_start": 1,
            "line_end": 50,
        },
        is_defer=False,
        d6_fired=False,
        absence_forced=False,
        counts={},
        formulated_count=0,
        tenant_id="tenant-a",
        investigation_id="inv-1",
    )
    for key, value in over.items():
        setattr(base, key, value)
    return base


def _evidenced(action, params=None, evidence_added=1):  # type: ignore[no-untyped-def]
    return {"action": action, "evidence_added": evidence_added, "parameters": params or {}}


class TestPivotDispatch:
    def test_pivot_dispatches_recollect_and_records_focus(self) -> None:
        out = apply_post_reason_cascade(_state())
        assert out.reason_data["next_action"] == "GET_CODE"
        assert out.reason_data["action_parameters"]["file_path"] == "svc/new.py"
        assert out.reason_data["forced_pivot_recollect"] is True
        assert out.prev_focus is not None
        assert out.prev_focus["surface"] == "svc/new.py"
        assert out.prev_focus["surface_kind"] == "file"
        assert out.prev_focus["line_start"] == 1

    def test_stable_focus_dispatches_untouched(self) -> None:
        state = _state(
            prev_focus={
                "surface": "svc/new.py",
                "surface_kind": "file",
                "line_start": 1,
                "line_end": 20,
            }
        )
        out = apply_post_reason_cascade(state)
        assert out.reason_data.get("forced_pivot_recollect") is not True
        assert out.reason_data["next_action"] == "GET_CODE"


class TestCompetingRules:
    def test_pivot_beats_formulate(self) -> None:
        state = _state(
            reason_data={
                "next_action": "GET_CODE",
                "action_parameters": {"file_path": "svc/new.py"},
                "hypotheses_evaluated": [{"statement": "s"}],
            }
        )
        out = apply_post_reason_cascade(state)
        assert out.reason_data["next_action"] == "GET_CODE"
        assert out.reason_data["forced_pivot_recollect"] is True
        assert out.reason_data.get("forced_formulate") is not True

    def test_pivot_beats_synthesis(self) -> None:
        state = _state(
            evidence_banked_total=9,
            action_history=[
                _evidenced("GET_CODE", {"file_path": "svc/old.py"}),
                _evidenced("SEARCH_CODE", {"query": "filter"}),
            ],
        )
        out = apply_post_reason_cascade(state)
        assert out.reason_data["forced_pivot_recollect"] is True
        assert out.reason_data.get("forced_formulate") is not True

    def test_pivot_beats_valid_carried_order(self) -> None:
        carrier = {
            "kind": "work_order",
            "order_kind": "REQUERY",
            "iteration": 1,
            "turn_issued": 1,
            "claim_ref": "c1",
            "query": "order filter",
            "scope": "",
            "max_attempts": 3,
            "consumed": False,
            "order": {
                "work_order_id": "00000000-0000-0000-0000-000000000001",
                "kind": "REQUERY",
                "action_type": "SEARCH_CODE",
                "action_parameters": {"query": "order filter"},
                "addresses": {
                    "triage_route": "BUG",
                    "claim_kind": "untyped",
                    "has_unbeaten_rival": False,
                },
                "cost_estimate": 1,
                "seed_source": "test:carrier",
                "status": "PENDING",
            },
        }
        state = _state(action_history=[carrier])
        out = apply_post_reason_cascade(state)
        assert out.reason_data["forced_pivot_recollect"] is True
        assert out.reason_data.get("forced_requery") is not True
        assert carrier.get("consumed") is not True
        assert out.consumed_order_ref is None

    def test_carried_order_dispatches_and_reports_ref(self) -> None:
        carrier = {
            "kind": "work_order",
            "order_kind": "REQUERY",
            "iteration": 1,
            "turn_issued": 1,
            "claim_ref": "c1",
            "query": "order filter",
            "scope": "",
            "max_attempts": 3,
            "consumed": False,
            "order": {
                "work_order_id": "00000000-0000-0000-0000-000000000001",
                "kind": "REQUERY",
                "action_type": "SEARCH_CODE",
                "action_parameters": {"query": "order filter"},
                "addresses": {
                    "triage_route": "BUG",
                    "claim_kind": "untyped",
                    "has_unbeaten_rival": False,
                },
                "cost_estimate": 1,
                "seed_source": "test:carrier",
                "status": "PENDING",
            },
        }
        state = _state(
            reason_data={"next_action": "GET_CODE", "action_parameters": {"file_path": "svc/old.py"}},
            prev_focus=None,
            action_history=[carrier],
        )
        out = apply_post_reason_cascade(state)
        assert out.reason_data.get("forced_requery") is True
        assert out.reason_data["action_parameters"] == {"query": "order filter"}
        assert out.consumed_order_ref is carrier

    def test_pivot_beats_callsite(self) -> None:
        state = _state(
            reason_data={
                "next_action": "GET_CODE",
                "action_parameters": {"file_path": "svc/new.py"},
                "claims": [
                    {
                        "claim_kind": "CALL_EDGE",
                        "target": {"file_path": "svc/caller.py", "symbol": "S"},
                    }
                ],
            }
        )
        out = apply_post_reason_cascade(state)
        assert out.reason_data["forced_pivot_recollect"] is True
        assert out.reason_data.get("forced_callsite") is not True

    def test_pivot_beats_log_search(self) -> None:
        state = _state(
            reason_data={
                "next_action": "FIND_SYMBOL",
                "action_parameters": {"symbol_name": "normalize"},
            },
            prev_focus={
                "surface": "svc/old.py",
                "surface_kind": "file",
                "line_start": 1,
                "line_end": 50,
            },
            description="service error failing with exception in logs",
        )
        out = apply_post_reason_cascade(state)
        assert out.reason_data["next_action"] == "FIND_SYMBOL"
        assert out.reason_data["forced_pivot_recollect"] is True
        assert out.reason_data.get("forced_log_search") is not True

    def test_pivot_beats_replan(self) -> None:
        history = [
            {"action": "SEARCH_CODE", "evidence_added": 0, "parameters": {"query": "x"}},
            {"action": "SEARCH_CODE", "evidence_added": 0, "parameters": {"query": "x"}},
        ]
        state = _state(
            reason_data={
                "next_action": "SEARCH_CODE",
                "action_parameters": {"file_path": "svc/new.py", "query": "filter"},
            },
            action_history=history,
            description="where is the order filter implemented",
        )
        out = apply_post_reason_cascade(state)
        assert out.reason_data["forced_pivot_recollect"] is True
        assert out.reason_data.get("forced_replan") is not True

    def test_pivot_beats_smallest_fetch(self) -> None:
        feed = {
            "kind": "claim_gap_feed",
            "iteration": 2,
            "gaps": [
                {
                    "gap_id": "g1",
                    "kind": "DEFINITION",
                    "target": {"file_path": "svc/old.py"},
                }
            ],
        }
        state = _state(action_history=[feed])
        out = apply_post_reason_cascade(state)
        assert out.reason_data["forced_pivot_recollect"] is True
        assert out.reason_data.get("forced_smallest_fetch") is not True
        assert out.counts.get("smallest_fetch_definition", 0) == 0

    def test_walk_stack_wins_over_pivot(self) -> None:
        state = _state(
            knowledge_seed={
                "frames": [{"file": "svc/a.py", "line": 42, "function": "price"}],
            }
        )
        out = apply_post_reason_cascade(state)
        assert out.reason_data["next_action"] == "WALK_STACK"
        assert out.reason_data.get("forced_walk_stack") is True

    def test_pre_cascade_outcomes_freeze_cascade(self) -> None:
        for flags in (
            {"is_defer": True},
            {"d6_fired": True},
            {"absence_forced": True},
        ):
            state = _state(
                reason_data={"next_action": "CONCLUDE", "forced_not_checkable": True},
                **flags,  # type: ignore[arg-type]
            )
            out = apply_post_reason_cascade(state)
            assert out.reason_data["next_action"] == "CONCLUDE"
            assert out.reason_data.get("forced_pivot_recollect") is not True


class TestDampingAndKinds:
    def test_symbol_refire_damped(self) -> None:
        history = [
            _evidenced("FIND_SYMBOL", {"symbol_name": "normalize"}),
        ]
        state = _state(
            reason_data={
                "next_action": "FIND_SYMBOL",
                "action_parameters": {"symbol_name": "normalize"},
            },
            prev_focus={
                "surface": "svc/a.py",
                "surface_kind": "file",
                "line_start": 1,
                "line_end": 10,
            },
            action_history=history,
        )
        out = apply_post_reason_cascade(state)
        assert out.reason_data.get("forced_pivot_recollect") is not True
        assert out.reason_data["next_action"] == "FIND_SYMBOL"

    def test_file_does_not_damp_symbol_pivot(self) -> None:
        history = [_evidenced("GET_CODE", {"file_path": "normalize"})]
        state = _state(
            reason_data={
                "next_action": "FIND_SYMBOL",
                "action_parameters": {"symbol_name": "normalize"},
            },
            prev_focus={
                "surface": "svc/a.py",
                "surface_kind": "file",
                "line_start": 1,
                "line_end": 10,
            },
            action_history=history,
        )
        out = apply_post_reason_cascade(state)
        assert out.reason_data["forced_pivot_recollect"] is True

    def test_kind_change_fires_pivot(self) -> None:
        state = _state(
            reason_data={
                "next_action": "FIND_SYMBOL",
                "action_parameters": {"symbol_name": "svc/new.py"},
            },
            prev_focus={
                "surface": "svc/new.py",
                "surface_kind": "file",
                "line_start": 1,
                "line_end": 20,
            },
        )
        out = apply_post_reason_cascade(state)
        assert out.reason_data["forced_pivot_recollect"] is True


class TestDeterminism:
    def test_same_inputs_identical_outputs(self) -> None:
        first = apply_post_reason_cascade(_state())
        second = apply_post_reason_cascade(_state())
        assert first.reason_data == second.reason_data
        assert first.prev_focus == second.prev_focus
        assert first.counts == second.counts
        assert first.consumed_order_ref == second.consumed_order_ref

    def test_focus_state_chains_across_turns(self) -> None:
        turn1 = apply_post_reason_cascade(_state())
        assert turn1.reason_data["forced_pivot_recollect"] is True
        # Same proposal next turn: no movement, no refire.
        turn2 = apply_post_reason_cascade(
            _state(prev_focus=turn1.prev_focus, iteration=4)
        )
        assert turn2.reason_data.get("forced_pivot_recollect") is not True
        # New surface on turn 3: fires again, damped only per surface.
        turn3 = apply_post_reason_cascade(
            _state(
                prev_focus=turn2.prev_focus,
                iteration=5,
                reason_data={
                    "next_action": "GET_CODE",
                    "action_parameters": {"file_path": "svc/third.py"},
                },
            )
        )
        assert turn3.reason_data["forced_pivot_recollect"] is True
        assert turn3.reason_data["action_parameters"]["file_path"] == "svc/third.py"


class TestSeamParity:
    def test_floor_still_fires_without_pivot(self) -> None:
        state = _state(
            reason_data={"next_action": "FORMULATE_HYPOTHESIS"},
            evidence_banked_total=1,
            knowledge_seed={"failing_symbol": "price"},
        )
        out = apply_post_reason_cascade(state)
        assert out.reason_data.get("forced_floor") is True
        assert out.reason_data["next_action"] != "FORMULATE_HYPOTHESIS"
