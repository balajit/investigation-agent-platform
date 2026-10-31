# tests/unit/test_w1_advisory_removal.py
"""W1-A: advisory layer deleted, dispatch unchanged (Sonnet W D6/D8).

W1-T01 parity corpus: dispatch outputs identical before/after deletion.
W1-T02/T03/T05/T06 source pins: advisory surface is gone.
"""

import dataclasses
import inspect

from investigation_agent_platform.application.worker.workflows import forcing
from investigation_agent_platform.application.worker.workflows.forcing import (
    PostReasonCascadeState,
    apply_post_reason_cascade,
)


def _state(**over):  # type: ignore[no-untyped-def]
    base = PostReasonCascadeState(
        reason_data={
            "next_action": "GET_CODE",
            "action_parameters": {"file_path": "svc/new.py", "start_line": 1, "end_line": 20},
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


def _dispatch_signature(out):  # type: ignore[no-untyped-def]
    rd = dict(out.reason_data or {})
    return (
        rd.get("next_action"),
        (rd.get("action_parameters") or {}).get("file_path"),
        bool(rd.get("forced_pivot_recollect")),
        bool(rd.get("forced_requery")),
        bool(rd.get("forced_smallest_fetch")),
        out.prev_focus,
        out.consumed_order_ref,
    )


class TestW1Parity:
    def test_dispatch_parity_file_pivot(self) -> None:
        out = apply_post_reason_cascade(_state())
        assert out.reason_data["forced_pivot_recollect"] is True
        assert out.reason_data["next_action"] == "GET_CODE"

    def test_dispatch_parity_formulate_no_force(self) -> None:
        out = apply_post_reason_cascade(
            _state(reason_data={"next_action": "FORMULATE_HYPOTHESIS"})
        )
        assert out.reason_data["next_action"] == "FORMULATE_HYPOTHESIS"

    def test_order_events_present_and_empty_by_default(self) -> None:
        out = apply_post_reason_cascade(_state())
        assert isinstance(out.order_events, list)
        assert out.order_events == []


class TestW1Removal:
    def test_no_advisory_fields(self) -> None:
        names = {f.name for f in dataclasses.fields(PostReasonCascadeState)}
        assert "advisory_orders" not in names
        assert "advisory_ranking" not in names
        assert "order_events" in names

    def test_removed_symbols_gone(self) -> None:
        assert not hasattr(forcing, "orders_from_cascade_signals")
        assert not hasattr(forcing, "rank_advisory_orders")
        assert not hasattr(forcing, "_order_id")

    def test_workflow_never_reads_advisory(self) -> None:
        import investigation_agent_platform.application.worker.workflows.run_investigation as ri

        assert "advisory" not in inspect.getsource(ri)

    def test_forcing_does_not_import_frontier_ranking(self) -> None:
        src = inspect.getsource(forcing)
        assert "order_candidates" not in src
        assert "FrontierEvidenceState" not in src
        assert "work_orders_to_seeds" not in src


class TestW1MigratedPrecedence:
    def test_requery_outranks_carried_gap(self) -> None:
        from investigation_agent_platform.application.worker.workflows.forcing import (
            _carry_gap_orders,
        )

        gap_carrier = _carry_gap_orders(
            {
                "claim_gaps": [
                    {"gap_id": "g1", "kind": "DEFINITION", "target": {"file_path": "svc/gap.py"}}
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
        state = _state(
            reason_data={"next_action": "FORMULATE_HYPOTHESIS"},
            prev_focus=None,
            evidence_banked_total=9,
            action_history=[carrier, gap_carrier],
            formulated_count=1,
        )
        out = apply_post_reason_cascade(state)
        assert out.reason_data.get("forced_requery") is True
        assert out.reason_data.get("forced_smallest_fetch") is not True
        assert out.consumed_order_ref is carrier
