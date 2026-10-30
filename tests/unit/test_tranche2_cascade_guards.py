# tests/unit/test_tranche2_cascade_guards.py
"""Tranche 2 cascade guard fixes (review F1, F3, F5, F6, F7).

Each test pins behavior that was wrong before the fix.
"""

from __future__ import annotations

import pytest

from investigation_agent_platform.application.worker.workflows import forcing
from investigation_agent_platform.application.worker.workflows.forcing import (
    PostReasonCascadeState,
    apply_post_reason_cascade,
)
from investigation_agent_platform.domain.investigation.loop_version import (
    LOOP_VERSION_CURRENT,
    LOOP_VERSION_LV2,
    UnknownLoopVersion,
    resolve_loop_version,
)


def _state(**over):  # type: ignore[no-untyped-def]
    base = PostReasonCascadeState(
        reason_data={"next_action": "VERIFY_HYPOTHESIS", "action_parameters": {}},
        action_history=[],
        evidence_banked_total=5,
        triage_route="BUG",
        description="where is the order filter implemented",
        knowledge_seed={},
        max_iterations=10,
        iteration=3,
        prev_focus=None,
        is_defer=False,
        d6_fired=False,
        absence_forced=False,
        counts={},
        formulated_count=1,
        tenant_id="tenant-a",
        investigation_id="inv-1",
    )
    for key, value in over.items():
        setattr(base, key, value)
    return base


def _requery_carrier(**overrides):  # type: ignore[no-untyped-def]
    entry: dict = {
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
    entry.update(overrides)
    return entry


class TestLoopVersionResolution:
    def test_unmarked_resolves_current(self) -> None:
        assert resolve_loop_version({}) == LOOP_VERSION_CURRENT
        assert resolve_loop_version(None) == LOOP_VERSION_CURRENT

    def test_pinned_versions_round_trip(self) -> None:
        assert resolve_loop_version({"loop_version": "lv2"}) == LOOP_VERSION_LV2

    def test_unknown_pin_fails_loudly(self) -> None:
        with pytest.raises(UnknownLoopVersion):
            resolve_loop_version({"loop_version": "lv99"})

    def test_retired_lv1_is_unknown(self) -> None:
        with pytest.raises(UnknownLoopVersion):
            resolve_loop_version({"loop_version": "lv1"})

    def test_current_is_the_guarded_version(self) -> None:
        assert LOOP_VERSION_CURRENT == LOOP_VERSION_LV2


class TestF3RequeryStandsDownOnDeferAndAbsence:
    @pytest.mark.parametrize("flag", ["is_defer", "absence_forced"])
    def test_current_version_stands_down(self, flag: str) -> None:
        state = _state(action_history=[_requery_carrier()], **{flag: True})
        out = apply_post_reason_cascade(state)
        assert out.reason_data["next_action"] == "VERIFY_HYPOTHESIS"
        assert not out.reason_data.get("forced_requery")
        # The carrier stays open for a later, non-forced turn.
        assert out.action_history[0]["consumed"] is False
        assert out.consumed_order_ref is None

    def test_ordinary_turn_still_dispatches_requery(self) -> None:
        out = apply_post_reason_cascade(_state(action_history=[_requery_carrier()]))
        assert out.reason_data["next_action"] == "SEARCH_CODE"
        assert out.reason_data["forced_requery"] is True


class TestF5WalkStackClearsPivotFlag:
    def test_walk_stack_winning_over_pivot_clears_pivot_flag(self) -> None:
        state = _state(
            reason_data={
                "next_action": "GET_CODE",
                "action_parameters": {"file_path": "svc/new.py", "start_line": 1, "end_line": 20},
            },
            prev_focus={
                "surface": "svc/old.py",
                "surface_kind": "file",
                "line_start": 1,
                "line_end": 50,
            },
            knowledge_seed={
                "frames": [{"file": "svc/boom.py", "line": 7, "function": "run"}],
                "error_class": "ValueError",
            },
        )
        out = apply_post_reason_cascade(state)
        assert out.reason_data["next_action"] == "WALK_STACK"
        assert out.reason_data["forced_walk_stack"] is True
        assert not out.reason_data.get("forced_pivot_recollect"), (
            "pivot did not dispatch, so history must not stamp it as dispatched"
        )


class TestF6FloorSkipsAlreadyReadTargets:
    DESCRIPTION = "where is src/app/Foo.java implemented in the service"

    def _floor(self, history):  # type: ignore[no-untyped-def]
        return forcing._explore_floor_target(
            {"next_action": "VERIFY_HYPOTHESIS"},
            0,
            "BUG",
            self.DESCRIPTION,
            {},
            0,
            history=history,
        )

    def test_unread_path_is_dispatched(self) -> None:
        floor = self._floor([])
        assert floor is not None
        assert floor["next_action"] == "GET_CODE"
        assert floor["action_parameters"]["file_path"] == "src/app/Foo.java"

    def test_already_read_path_is_not_repeated(self) -> None:
        history = [
            {
                "action": "GET_CODE",
                "evidence_added": 1,
                "parameters": {"file_path": "src/app/Foo.java"},
            }
        ]
        floor = self._floor(history)
        assert not (
            floor is not None
            and floor["next_action"] == "GET_CODE"
            and floor["action_parameters"].get("file_path") == "src/app/Foo.java"
        )

    def test_leading_dot_slash_is_normalised_for_dedupe(self) -> None:
        history = [
            {
                "action": "GET_CODE",
                "evidence_added": 1,
                "parameters": {"file_path": "./src/app/Foo.java"},
            }
        ]
        floor = self._floor(history)
        assert not (floor is not None and floor["next_action"] == "GET_CODE")

    def test_cascade_passes_history_to_floor(self) -> None:
        history = [
            {
                "action": "GET_CODE",
                "evidence_added": 1,
                "parameters": {"file_path": "src/app/Foo.java"},
            }
        ]
        out = apply_post_reason_cascade(
            _state(
                reason_data={"next_action": "VERIFY_HYPOTHESIS", "s4_ready": False},
                description=self.DESCRIPTION,
                evidence_banked_total=1,
                action_history=history,
            )
        )
        params = out.reason_data.get("action_parameters") or {}
        assert not (
            out.reason_data.get("forced_floor") and params.get("file_path") == "src/app/Foo.java"
        )


class TestF7CascadeFaultDoesNotBurnCounters:
    def test_exception_leaves_counters_and_carrier_untouched(self, monkeypatch, caplog) -> None:
        # Walk-stack fires (increments its counter) and a later rule raises.
        def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("late rule fault")

        monkeypatch.setattr(forcing, "_should_force_formulate", _boom)
        counts: dict[str, int] = {}
        state = _state(
            reason_data={"next_action": "GET_CODE", "action_parameters": {"file_path": "a.py"}},
            knowledge_seed={"frames": [{"file": "svc/boom.py", "line": 7, "function": "run"}]},
            counts=counts,
        )
        with caplog.at_level("WARNING"):
            out = apply_post_reason_cascade(state)
        assert out.reason_data["next_action"] == "GET_CODE", "proposal must pass through unchanged"
        assert counts == {}, f"counter burned without a dispatch: {counts}"
        assert any("cascade" in r.message.lower() for r in caplog.records), "fault must be logged"

    def test_carrier_is_not_stamped_consumed_when_cascade_faults(self, monkeypatch) -> None:
        def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("late rule fault")

        monkeypatch.setattr(forcing, "_should_force_formulate", _boom)
        # A non-PENDING order takes the in-place "consume" path.
        carrier = _requery_carrier()
        carrier["order"]["status"] = "EXPIRED"
        out = apply_post_reason_cascade(_state(action_history=[carrier]))
        assert out.reason_data["next_action"] == "VERIFY_HYPOTHESIS"
        assert carrier["consumed"] is False, "stamp must commit only if the cascade succeeds"

    def test_consume_stamp_commits_on_success(self) -> None:
        carrier = _requery_carrier()
        carrier["order"]["status"] = "EXPIRED"
        out = apply_post_reason_cascade(_state(action_history=[carrier]))
        assert out.reason_data["next_action"] == "VERIFY_HYPOTHESIS"
        assert carrier["consumed"] is True

    def test_success_commits_counter_exactly_once(self) -> None:
        counts: dict[str, int] = {}
        state = _state(
            reason_data={"next_action": "GET_CODE", "action_parameters": {"file_path": "a.py"}},
            knowledge_seed={"frames": [{"file": "svc/boom.py", "line": 7, "function": "run"}]},
            counts=counts,
        )
        out = apply_post_reason_cascade(state)
        assert out.reason_data["next_action"] == "WALK_STACK"
        assert counts.get("walk_stack") == 1


class TestF1HistoryRecordsDispatchedParameters:
    def test_dispatched_parameters_follow_the_override_not_the_proposal(self) -> None:
        proposal = {"file_path": "svc/proposed.py"}
        state = _state(
            reason_data={"next_action": "GET_CODE", "action_parameters": proposal},
            knowledge_seed={"frames": [{"file": "svc/boom.py", "line": 7, "function": "run"}]},
        )
        out = apply_post_reason_cascade(state)
        recorded = forcing.dispatched_parameters(out.reason_data)
        assert "frames" in recorded
        assert recorded.get("file_path") != "svc/proposed.py"

    def test_dispatched_parameters_tolerates_bad_shapes(self) -> None:
        assert forcing.dispatched_parameters(None) == {}
        assert forcing.dispatched_parameters({"action_parameters": "x"}) == {}

    def test_workflow_history_uses_dispatched_parameters(self) -> None:
        import inspect

        from investigation_agent_platform.application.worker.workflows import run_investigation

        source = inspect.getsource(run_investigation)
        assert "dispatched_parameters(_reason_data)" in source
        assert '_params = dict(reason_res.data.get("action_parameters"' not in source
