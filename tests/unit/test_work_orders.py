# tests/unit/test_work_orders.py
"""P6 chunk 2a: typed work orders + translation (domain only). No loop,
no model calls."""

import pytest

from investigation_agent_platform.domain.collection.frontier import (
    EvidenceState,
    FailureCharacter,
    FrontierDimension,
    FrontierExit,
    evaluate_exit,
    order_candidates,
)
from investigation_agent_platform.domain.collection.work_orders import (
    MAX_WORK_ORDER_SEEDS,
    WORK_ORDER_VERSION_CURRENT,
    WorkOrder,
    WorkOrderKind,
    WorkOrderStatus,
    work_orders_to_seeds,
)


def _character(**kw):
    args = {"triage_route": "BUG", "claim_kind": "BEHAVIOR", "has_unbeaten_rival": False}
    args.update(kw)
    return FailureCharacter(**args)


def _order(kind=WorkOrderKind.FETCH_SEED, params=None, **kw):
    args = {
        "kind": kind,
        "action_type": "GET_CODE",
        "action_parameters": dict(params or {}),
        "addresses": _character(),
        "cost_estimate": 3,
    }
    args.update(kw)
    return WorkOrder(**args)


class TestWorkOrderContract:
    def test_version_pin(self):
        assert WORK_ORDER_VERSION_CURRENT == "workorder-v1"
        assert _order().version == "workorder-v1"

    def test_rejects_empty_action_type(self):
        with pytest.raises(ValueError):
            WorkOrder(
                kind=WorkOrderKind.FETCH_SEED,
                action_type="",
                action_parameters={"file_path": "a.py"},
                addresses=_character(),
                cost_estimate=0,
            )

    def test_rejects_non_json_parameters(self):
        with pytest.raises(ValueError):
            WorkOrder(
                kind=WorkOrderKind.FETCH_SEED,
                action_type="GET_CODE",
                action_parameters={"cb": object()},
                addresses=_character(),
                cost_estimate=0,
            )

    def test_all_kinds_construct(self):
        for kind in WorkOrderKind:
            assert _order(kind=kind).kind == kind


class TestTranslation:
    def test_fetch_seed_carries_identity_fetch_and_cost(self):
        order = _order(
            params={
                "file_path": "tenant/svc/a.py",
                "start_line": 10,
                "end_line": 20,
                "revision": "abc123",
            }
        )
        seeded, skipped = work_orders_to_seeds([order], _character(), EvidenceState.NO_ROWS)
        assert skipped == []
        assert len(seeded) == 1
        candidate = seeded[0]
        assert candidate.file_path == "tenant/svc/a.py"
        assert candidate.span == "10-20"
        assert candidate.commit == "abc123"
        assert candidate.seed_fetch.fetch_kind == "GET_CODE"
        assert candidate.seed_fetch.target == "tenant/svc/a.py"
        assert candidate.cost_estimate == 3
        assert candidate.dimension == FrontierDimension.LAYER_SPREAD
        assert candidate.dispatchable is True

    def test_query_only_order_skips_never_guesses(self):
        order = _order(params={"query": "OrderServiceHandler"})
        seeded, skipped = work_orders_to_seeds([order], _character(), EvidenceState.NO_ROWS)
        assert seeded == []
        assert skipped == [order.work_order_id]

    def test_non_pending_orders_skip(self):
        consumed = _order(status=WorkOrderStatus.CONSUMED, params={"file_path": "a.py"})
        expired = _order(status=WorkOrderStatus.EXPIRED, params={"file_path": "b.py"})
        seeded, skipped = work_orders_to_seeds(
            [consumed, expired], _character(), EvidenceState.NO_ROWS
        )
        assert seeded == []
        assert skipped == [consumed.work_order_id, expired.work_order_id]

    def test_local_complete_seeds_nothing(self):
        order = _order(params={"file_path": "a.py"})
        seeded, skipped = work_orders_to_seeds([order], _character(), EvidenceState.LOCAL_COMPLETE)
        assert seeded == []
        assert skipped == [order.work_order_id]

    def test_seed_cap_is_first_n_deterministic(self):
        assert MAX_WORK_ORDER_SEEDS == 8
        orders = [_order(params={"file_path": f"tenant/svc/m{i}.py"}) for i in range(10)]
        seeded, skipped = work_orders_to_seeds(orders, _character(), EvidenceState.NO_ROWS)
        assert len(seeded) == MAX_WORK_ORDER_SEEDS
        assert len(skipped) == 2
        again, _ = work_orders_to_seeds(orders, _character(), EvidenceState.NO_ROWS)
        assert [c.seed_source for c in seeded] == [c.seed_source for c in again]

    def test_dimension_follows_character(self):
        order = _order(params={"file_path": "a.py"})
        seeded, _ = work_orders_to_seeds(
            [order], _character(has_unbeaten_rival=True), EvidenceState.NO_ROWS
        )
        assert seeded[0].dimension == FrontierDimension.RIVAL_SPREAD


class TestTranslatedSeedsOrder:
    def test_seeds_flow_into_best_first_ordering(self):
        order = _order(params={"file_path": "tenant/svc/a.py", "start_line": 1, "end_line": 5})
        seeded, _ = work_orders_to_seeds([order], _character(), EvidenceState.NO_ROWS)
        ordered = order_candidates(seeded, [])
        assert [c.candidate_id for c in ordered] == [c.candidate_id for c in seeded]
        assert (
            evaluate_exit(
                spread_budget_remaining=50,
                ordered_pending=ordered,
                top_gain_history=[],
                independent_artifact_types=0,
                posterior=0.0,
            )
            == FrontierExit.CONTINUE
        )
