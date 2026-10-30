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
        new_order = _order(
            action_parameters={"file_path": "svc/new.py"}, seed_source="test:new"
        )
        old_order = _order(
            action_parameters={"file_path": "svc/old.py"}, seed_source="test:old"
        )
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
        assert rank_advisory_orders("junk", "junk", "junk")["ranked"] == []  # type: ignore[arg-type]
        out = rank_advisory_orders([], [{"action": "GET_CODE"}], -1)
        assert out["ranked"] == []


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

    def test_gap_and_marker_turn_ranks_both(self) -> None:
        feed = {
            "kind": "claim_gap_feed",
            "iteration": 2,
            "gaps": [
                {
                    "gap_id": "g1",
                    "kind": "DEFINITION",
                    "target": {"file_path": "svc/gap.py"},
                }
            ],
        }
        marker = {
            "kind": "ledger_requery_directive",
            "query": "order filter",
            "scope": "",
            "action_type": "SEARCH_CODE",
            "action_parameters": {"query": "order filter"},
            "claim_ref": "c1",
            "turn_issued": 1,
            "max_attempts": 3,
        }
        state = self._base_state(
            reason_data={"next_action": "FORMULATE_HYPOTHESIS"},
            prev_focus=None,
            evidence_banked_total=9,
            action_history=[marker, feed],
            formulated_count=1,
        )
        out = apply_post_reason_cascade(state)
        kinds = sorted(o["kind"] for o in out.advisory_orders)
        assert kinds == ["GAP_FILL", "REQUERY"]
        ranked_sources = sorted(r["seed_source"] for r in out.advisory_ranking["ranked"])
        # The query-only REQUERY has no file identity → skipped, recorded.
        assert ranked_sources == ["gap:g1"]
        assert out.advisory_ranking["skipped"] != []
        # Legacy dispatch untouched by the ranking: the valid marker wins
        # the cascade (ledger outranks smallest-fetch) exactly as before.
        assert out.reason_data.get("forced_requery") is True
        assert out.reason_data.get("forced_smallest_fetch") is not True
