# tests/unit/test_work_order_emission.py
"""2b steps 3/5/6: typed work orders from legacy signals, advisory only.

Pins the emission mapping (pivot → FETCH_SEED), determinism, bounds,
and lifecycle separation (orders stay PENDING; the cascade dispatches
unchanged). Since steps 5-6 REQUERY and GAP_FILL orders arrive via
carried order entries and dispatch from their slots — emission ignores
carriers and stale marker/feed dicts.
"""

import json

from investigation_agent_platform.application.worker.workflows.forcing import (
    PostReasonCascadeState,
    apply_post_reason_cascade,
    orders_from_cascade_signals,
)


def _carrier(**over):  # type: ignore[no-untyped-def]
    base = {
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
    base.update(over)
    return base


def _gap(**over):  # type: ignore[no-untyped-def]
    base = {
        "gap_id": "g1",
        "kind": "DEFINITION",
        "claim_ref": "ck1",
        "target": {"file_path": "svc/old.py", "line_start": 10, "line_end": 20},
    }
    base.update(over)
    return base


def _feed(*gaps):  # type: ignore[no-untyped-def]
    return {"kind": "claim_gap_feed", "iteration": 2, "gaps": list(gaps)}


class TestRequeryEmission:
    def test_carriers_emit_nothing_from_emission(self) -> None:
        # REQUERY orders arrive via carriers and dispatch from the G3
        # slot; emission ignores them (consumed or not).
        assert orders_from_cascade_signals([_carrier()], None, "BUG") == []
        assert orders_from_cascade_signals([_carrier(consumed=True)], None, "BUG") == []

    def test_stale_marker_dicts_emit_nothing(self) -> None:
        # Pre-step-5 marker dicts are dead shapes — never resurrected.
        stale = {
            "kind": "ledger_requery_directive",
            "query": "order filter",
            "action_type": "SEARCH_CODE",
            "action_parameters": {"query": "order filter"},
        }
        assert orders_from_cascade_signals([stale], None, "BUG") == []


class TestGapFillEmission:
    def test_feed_entries_emit_nothing_from_emission(self) -> None:
        # Gap orders arrive via carriers and dispatch from the
        # smallest-fetch slot; emission ignores them.
        assert orders_from_cascade_signals([_feed(_gap())], None, "BUG") == []

    def test_stale_feed_shapes_emit_nothing(self) -> None:
        # Pre-step-6 feed dicts are dead shapes — never resurrected.
        assert orders_from_cascade_signals([_feed()], None, "BUG") == []
        assert orders_from_cascade_signals([_feed(_gap(kind=""))], None, "BUG") == []
        assert (
            orders_from_cascade_signals(
                [_feed(_gap(target={}, query="", scope=""))], None, "BUG"
            )
            == []
        )

    def test_carriers_emit_nothing_from_emission(self) -> None:
        from investigation_agent_platform.application.worker.workflows.forcing import (
            _carry_gap_orders,
        )

        carrier = _carry_gap_orders({"claim_gaps": [_gap()]}, 2)
        assert carrier is not None
        assert orders_from_cascade_signals([carrier], None, "BUG") == []


class TestFetchSeedEmission:
    def test_file_pivot_emits_fetch_seed(self) -> None:
        pivot = {
            "surface": "svc/new.py",
            "surface_kind": "file",
            "next_action": "GET_CODE",
            "action_parameters": {"file_path": "svc/new.py", "revision": "HEAD"},
        }
        orders = orders_from_cascade_signals([], pivot, "BUG")
        assert len(orders) == 1
        assert orders[0].kind.value == "FETCH_SEED"
        assert orders[0].action_type == "GET_CODE"
        assert orders[0].seed_source == "pivot:file:svc/new.py"

    def test_symbol_pivot_emits_find_symbol_seed(self) -> None:
        pivot = {
            "surface": "normalize",
            "surface_kind": "symbol",
            "next_action": "FIND_SYMBOL",
            "action_parameters": {"symbol_name": "normalize"},
        }
        orders = orders_from_cascade_signals([], pivot, "BUG")
        assert len(orders) == 1
        assert orders[0].action_type == "FIND_SYMBOL"

    def test_no_pivot_no_seed(self) -> None:
        assert orders_from_cascade_signals([], None, "BUG") == []


class TestEmissionProperties:
    def test_deterministic_ids(self) -> None:
        history = [_feed(_gap())]
        pivot = {
            "surface": "svc/new.py",
            "surface_kind": "file",
            "next_action": "GET_CODE",
            "action_parameters": {"file_path": "svc/new.py"},
        }
        first = orders_from_cascade_signals(history, pivot, "BUG")
        second = orders_from_cascade_signals(history, pivot, "BUG")
        assert [str(o.work_order_id) for o in first] == [str(o.work_order_id) for o in second]
        assert [o.model_dump(mode="json") for o in first] == [
            o.model_dump(mode="json") for o in second
        ]

    def test_json_serializable(self) -> None:
        history = [_feed(_gap())]
        orders = orders_from_cascade_signals(history, None, "BUG")
        json.dumps([o.model_dump(mode="json") for o in orders])

    def test_junk_inputs_emit_nothing(self) -> None:
        assert orders_from_cascade_signals(None, None, None) == []
        assert orders_from_cascade_signals("junk", "junk", "junk") == []  # type: ignore[arg-type]


class TestSeamIntegration:
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

    def test_pivot_turn_records_seed_without_dispatch_change(self) -> None:
        out = apply_post_reason_cascade(self._base_state())
        assert out.reason_data["forced_pivot_recollect"] is True
        kinds = [o["kind"] for o in out.advisory_orders]
        assert kinds == ["FETCH_SEED"]
        assert out.advisory_orders[0]["seed_source"] == "pivot:file:svc/new.py"

    def test_carrier_and_gap_recorded_while_pivot_dispatches(self) -> None:
        state = self._base_state(
            action_history=[_carrier(), _feed(_gap(gap_id="g9"))],
        )
        out = apply_post_reason_cascade(state)
        # Dispatch is the pivot path (pivot wins, no requery/smallest flags).
        assert out.reason_data["forced_pivot_recollect"] is True
        assert out.reason_data.get("forced_requery") is not True
        assert out.reason_data.get("forced_smallest_fetch") is not True
        kinds = sorted(o["kind"] for o in out.advisory_orders)
        assert kinds == ["FETCH_SEED"]

    def test_gap_carrier_dispatches_without_advisory_duplicate(self) -> None:
        from investigation_agent_platform.application.worker.workflows.forcing import (
            _carry_gap_orders,
        )

        carrier = _carry_gap_orders({"claim_gaps": [_gap(gap_id="g7")]}, 2)
        assert carrier is not None
        state = self._base_state(
            reason_data={"next_action": "FORMULATE_HYPOTHESIS"},
            prev_focus=None,
            evidence_banked_total=9,
            action_history=[carrier],
            formulated_count=1,
        )
        out = apply_post_reason_cascade(state)
        # The slot dispatches the carried order; emission holds no copy.
        assert out.reason_data.get("forced_smallest_fetch") is True
        assert out.advisory_orders == []
