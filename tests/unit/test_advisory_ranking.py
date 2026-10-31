# tests/unit/test_advisory_ranking.py
"""2b step 4: advisory frontier ranking — record without dispatch.

Pins translation of advisory orders to seeds, best-first ordering,
determinism, bounds, and negative probes. The cascade never consumes
the ranking; dispatch behavior is pinned unchanged by the seam tests.
"""

import json
from uuid import uuid4

from investigation_agent_platform.application.worker.workflows.forcing import (
    PostReasonCascadeState,
    apply_post_reason_cascade,
    rank_advisory_orders,
)
from investigation_agent_platform.domain.collection.work_orders import (
    FailureCharacter,
    WorkOrder,
    WorkOrderKind,
    WorkOrderStatus,
)


def _order(**over):  # type: ignore[no-untyped-def]
    base = WorkOrder(
        work_order_id=uuid4(),
        kind=WorkOrderKind.GAP_FILL,
        action_type="GET_CODE",
        action_parameters={"file_path": "svc/new.py"},
        addresses=FailureCharacter(triage_route="BUG"),
        cost_estimate=1,
        seed_source="test:seed",
        status=WorkOrderStatus.PENDING,
    )
    if not over:
        return base
    return base.model_copy(update=over)


def _history_get(path, start=None, end=None, evidenced=1):  # type: ignore[no-untyped-def]
    params: dict = {"file_path": path}
    if start is not None:
        params["start_line"] = start
    if end is not None:
        params["end_line"] = end
    return {"action": "GET_CODE", "evidence_added": evidenced, "parameters": params}


class TestRanking:
    def test_novel_file_outranks_collected_file(self) -> None:
        history = [_history_get("svc/old.py")]
        new_order = _order(action_parameters={"file_path": "svc/new.py"}, seed_source="test:new")
        old_order = _order(action_parameters={"file_path": "svc/old.py"}, seed_source="test:old")
        ranking = rank_advisory_orders([old_order, new_order], history, 5)
        assert ranking["evidence_state"] == "ROWS_WITHOUT_MATCH"
        assert [r["file_path"] for r in ranking["ranked"]] == ["svc/new.py", "svc/old.py"]
        assert ranking["ranked"][0]["rank"] == 1
        assert ranking["skipped"] == []

    def test_fully_collected_span_dropped(self) -> None:
        history = [_history_get("svc/old.py", 1, 20)]
        order = _order(
            action_parameters={"file_path": "svc/old.py", "start_line": 1, "end_line": 20}
        )
        ranking = rank_advisory_orders([order], history, 5)
        assert ranking["ranked"] == []

    def test_deterministic_across_calls(self) -> None:
        history = [_history_get("svc/old.py")]
        orders = [
            _order(action_parameters={"file_path": "svc/new.py"}, seed_source="test:new"),
            _order(action_parameters={"file_path": "svc/old.py"}, seed_source="test:old"),
        ]
        first = rank_advisory_orders(orders, history, 5)
        second = rank_advisory_orders(orders, history, 5)
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    def test_empty_orders_empty_ranking(self) -> None:
        assert rank_advisory_orders([], [], 0)["ranked"] == []
        assert rank_advisory_orders([], [], 0)["evidence_state"] == "NO_ROWS"
        assert rank_advisory_orders([], [], 3)["evidence_state"] == "ROWS_WITHOUT_MATCH"

    def test_unlocatable_order_skipped_with_id(self) -> None:
        order = _order(
            kind=WorkOrderKind.REQUERY,
            action_type="SEARCH_CODE",
            action_parameters={"query": "filter"},
            seed_source="test:query-only",
        )
        ranking = rank_advisory_orders([order], [], 5)
        assert ranking["ranked"] == []
        assert ranking["skipped"] == [order.work_order_id.hex]

    def test_over_cap_overflow_skipped(self) -> None:
        orders = [
            _order(
                action_parameters={"file_path": f"svc/f{i}.py"},
                seed_source=f"test:f{i}",
            )
            for i in range(9)
        ]
        ranking = rank_advisory_orders(orders, [], 9)
        assert len(ranking["ranked"]) == 8
        assert len(ranking["skipped"]) == 1

    def test_junk_inputs_never_raise(self) -> None:
        assert rank_advisory_orders(None, None, 0)["ranked"] == []  # type: ignore[arg-type]
        assert rank_advisory_orders([], "junk", "junk")["ranked"] == []  # type: ignore[arg-type]
        out = rank_advisory_orders([], [{"action": "GET_CODE"}], -1)
        assert out["ranked"] == []


class TestRankingCap:
    def _pending(self, path: str) -> WorkOrder:
        return WorkOrder(
            work_order_id=uuid4(),
            kind=WorkOrderKind.GAP_FILL,
            action_type="GET_CODE",
            action_parameters={"file_path": path},
            addresses=FailureCharacter(triage_route="BUG"),
            cost_estimate=1,
            seed_source=f"test:{path}",
            status=WorkOrderStatus.PENDING,
        )

    def test_mixed_pending_nonpending_at_cap(self) -> None:
        pendings = [self._pending(f"svc/f{i}.py") for i in range(9)]
        spent_first = self._pending("svc/g0.py").model_copy(
            update={"status": WorkOrderStatus.CONSUMED}
        )
        spent_last = self._pending("svc/g1.py").model_copy(
            update={"status": WorkOrderStatus.EXPIRED}
        )
        orders = [spent_first, *pendings[:8], pendings[8], spent_last]
        first = rank_advisory_orders(orders, [], 9)
        # Cap is 8: the 8 pendings fill it, the 9th pending overflows,
        # and both non-pending orders skip closed without consuming
        # budget (one before and one after the cap fills).
        assert len(first["ranked"]) == 8
        assert len(first["skipped"]) == 3
        ranked_ids = {r["order_id"] for r in first["ranked"]}
        assert set(first["skipped"]).isdisjoint(ranked_ids)
        assert ranked_ids | set(first["skipped"]) == {o.work_order_id.hex for o in orders}
        assert spent_first.work_order_id.hex in first["skipped"]
        assert spent_last.work_order_id.hex in first["skipped"]
        assert pendings[8].work_order_id.hex in first["skipped"]
        # Deterministic at the cap: identical input, identical record.
        second = rank_advisory_orders(orders, [], 9)
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


class TestSeamRanking:
    def _base_state(self, **over):  # type: ignore[no-untyped-def]
        base = PostReasonCascadeState(
            reason_data={
                "next_action": "GET_CODE",
                "action_parameters": {"file_path": "svc/new.py"},
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

    def test_focus_tracks_dispatched_fetch_not_proposal(self) -> None:
        # lv6 and older (F4 changed this from lv7): the model proposes
        # FORMULATE (no focus); the cascade forces the smallest open gap
        # fetch. Recorded focus is the dispatched fetch, per the original
        # post-cascade contract in forcing.py.
        from investigation_agent_platform.application.worker.workflows.forcing import (
            _carry_gap_orders,
        )

        carrier = _carry_gap_orders(
            {
                "claim_gaps": [
                    {
                        "gap_id": "g1",
                        "kind": "DEFINITION",
                        "target": {"file_path": "svc/gap.py"},
                    },
                ]
            },
            2,
        )
        assert carrier is not None
        state = self._base_state(
            reason_data={"next_action": "FORMULATE_HYPOTHESIS"},
            action_history=[carrier],
            formulated_count=1,
            loop_version="lv6",
        )
        out = apply_post_reason_cascade(state)
        assert out.reason_data.get("forced_smallest_fetch") is True
        assert out.prev_focus is not None
        assert out.prev_focus["surface"] == "svc/gap.py"
        assert out.prev_focus["surface_kind"] == "file"

    def test_from_lv7_a_forced_fetch_does_not_become_the_focus_anchor(self) -> None:
        # F4: the same turn under lv7 keeps the model's own focus. A fetch
        # another rule forced must not make the next proposal look like a pivot.
        from investigation_agent_platform.application.worker.workflows.forcing import (
            _carry_gap_orders,
        )

        carrier = _carry_gap_orders(
            {
                "claim_gaps": [
                    {
                        "gap_id": "g1",
                        "kind": "DEFINITION",
                        "target": {"file_path": "svc/gap.py"},
                    },
                ]
            },
            2,
        )
        assert carrier is not None
        state = self._base_state(
            reason_data={"next_action": "FORMULATE_HYPOTHESIS"},
            action_history=[carrier],
            formulated_count=1,
            loop_version="lv7",
        )
        out = apply_post_reason_cascade(state)
        assert out.reason_data.get("forced_smallest_fetch") is True
        assert out.prev_focus is not None
        assert out.prev_focus["surface"] == "svc/old.py", "anchor stays on the model's focus"

    def test_pivot_turn_ranks_seed_first(self) -> None:
        out = apply_post_reason_cascade(self._base_state())
        ranking = out.advisory_ranking
        assert ranking["evidence_state"] == "ROWS_WITHOUT_MATCH"
        assert len(ranking["ranked"]) == 1
        top = ranking["ranked"][0]
        assert top["rank"] == 1
        assert top["file_path"] == "svc/new.py"
        assert top["fetch_kind"] == "GET_CODE"
        # Dispatch is the legacy pivot path; the ranking rides along.
        assert out.reason_data["forced_pivot_recollect"] is True
        assert "advisory" not in json.dumps(out.reason_data)

    def test_gap_carrier_turn_dispatches_without_advisory_copy(self) -> None:
        from investigation_agent_platform.application.worker.workflows.forcing import (
            _carry_gap_orders,
        )

        gap_carrier = _carry_gap_orders(
            {
                "claim_gaps": [
                    {
                        "gap_id": "g1",
                        "kind": "DEFINITION",
                        "target": {"file_path": "svc/gap.py"},
                    }
                ]
            },
            2,
        )
        assert gap_carrier is not None
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
                "action_type": "GET_CODE",
                "action_parameters": {"file_path": "svc/req.py"},
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
        state = self._base_state(
            reason_data={"next_action": "FORMULATE_HYPOTHESIS"},
            prev_focus=None,
            evidence_banked_total=9,
            action_history=[carrier, gap_carrier],
            formulated_count=1,
        )
        out = apply_post_reason_cascade(state)
        # The REQUERY carrier wins the G3 slot (order outranks
        # smallest-fetch) exactly as before; the gap order waits for a
        # turn with no requery, and emission holds neither.
        assert out.advisory_orders == []
        assert out.reason_data.get("forced_requery") is True
        assert out.reason_data.get("forced_smallest_fetch") is not True
        assert out.consumed_order_ref is carrier
