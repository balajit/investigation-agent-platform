# tests/unit/test_work_order_emission.py
"""2b step 3: typed work orders emitted from legacy signals, advisory only.

Pins the emission mapping (ledger markers → REQUERY, gap feeds →
GAP_FILL, pivot → FETCH_SEED), determinism, bounds, and lifecycle
separation (orders stay PENDING; the cascade dispatches the legacy
path unchanged).
"""

import json

from investigation_agent_platform.application.worker.workflows.forcing import (
    PostReasonCascadeState,
    apply_post_reason_cascade,
    orders_from_cascade_signals,
)


def _marker(**over):  # type: ignore[no-untyped-def]
    base = {
        "kind": "ledger_requery_directive",
        "query": "order filter",
        "scope": "",
        "action_type": "SEARCH_CODE",
        "action_parameters": {"query": "order filter"},
        "claim_ref": "c1",
        "turn_issued": 1,
        "max_attempts": 3,
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
    def test_valid_marker_emits_requery(self) -> None:
        orders = orders_from_cascade_signals([_marker()], None, "BUG")
        assert len(orders) == 1
        order = orders[0]
        assert order.kind.value == "REQUERY"
        assert order.action_type == "SEARCH_CODE"
        assert order.action_parameters == {"query": "order filter"}
        assert order.status.value == "PENDING"
        assert order.max_attempts == 3
        assert order.seed_source == "ledger:0"

    def test_consumed_and_invalid_markers_emit_nothing(self) -> None:
        history = [
            _marker(consumed=True),
            _marker(query=""),
            _marker(action_type="NOPE"),
            _marker(action_parameters={"nested": {"a": 1}}),
        ]
        assert orders_from_cascade_signals(history, None, "BUG") == []

    def test_bounded_newest_first(self) -> None:
        history = [
            _marker(query=f"q{i}", action_parameters={"query": f"q{i}"}) for i in range(6)
        ]
        orders = orders_from_cascade_signals(history, None, "BUG")
        assert len(orders) == 5
        assert orders[0].seed_source == "ledger:5"
        assert orders[-1].seed_source == "ledger:1"


class TestGapFillEmission:
    def test_definition_gap_emits_gap_fill(self) -> None:
        orders = orders_from_cascade_signals([_feed(_gap())], None, "BUG")
        assert len(orders) == 1
        order = orders[0]
        assert order.kind.value == "GAP_FILL"
        assert order.action_type == "GET_CODE"
        assert order.action_parameters["file_path"] == "svc/old.py"
        assert order.addresses.claim_kind == "DEFINITION"
        assert order.seed_source == "gap:g1"

    def test_latest_feed_only_and_cap(self) -> None:
        old = _feed(_gap(gap_id="old"))
        new_gaps = [_gap(gap_id=f"g{i}") for i in range(10)]
        orders = orders_from_cascade_signals([old, _feed(*new_gaps)], None, "BUG")
        assert len(orders) == 8
        assert all(o.seed_source != "gap:old" for o in orders)

    def test_empty_or_paramless_gaps_emit_nothing(self) -> None:
        assert orders_from_cascade_signals([_feed()], None, "BUG") == []
        assert orders_from_cascade_signals([_feed(_gap(kind=""))], None, "BUG") == []
        assert (
            orders_from_cascade_signals(
                [_feed(_gap(target={}, query="", scope=""))], None, "BUG"
            )
            == []
        )


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
        history = [_marker(), _feed(_gap())]
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
        history = [_marker(), _feed(_gap())]
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

    def test_marker_and_gap_recorded_while_pivot_dispatches(self) -> None:
        state = self._base_state(
            action_history=[_marker(), _feed(_gap(gap_id="g9"))],
        )
        out = apply_post_reason_cascade(state)
        # Dispatch is the legacy path (pivot wins, no requery/smallest flags).
        assert out.reason_data["forced_pivot_recollect"] is True
        assert out.reason_data.get("forced_requery") is not True
        assert out.reason_data.get("forced_smallest_fetch") is not True
        kinds = sorted(o["kind"] for o in out.advisory_orders)
        assert kinds == ["FETCH_SEED", "GAP_FILL", "REQUERY"]

    def test_gap_only_turn_records_without_interference(self) -> None:
        state = self._base_state(
            reason_data={"next_action": "FORMULATE_HYPOTHESIS"},
            prev_focus={
                "surface": "svc/old.py",
                "surface_kind": "file",
                "line_start": 1,
                "line_end": 50,
            },
            evidence_banked_total=9,
            action_history=[_feed(_gap(gap_id="g7"))],
            formulated_count=1,
        )
        out = apply_post_reason_cascade(state)
        # Legacy path dispatches the gap fill as before; advisory records it.
        assert out.reason_data.get("forced_smallest_fetch") is True
        assert [o["kind"] for o in out.advisory_orders] == ["GAP_FILL"]
