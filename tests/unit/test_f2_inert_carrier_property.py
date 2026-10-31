# tests/unit/test_f2_inert_carrier_property.py
"""F2-D21: inert carriers never change a cascade decision (lv6+); lv5 is the negative control."""

import copy
import random

from investigation_agent_platform.application.worker.workflows.forcing import (
    PostReasonCascadeState,
    apply_post_reason_cascade,
)

INERT = [
    {"kind": "work_order_feed", "order_kind": "GAP_FILL", "iteration": 2, "turn_issued": 2, "orders": []},
    {"kind": "work_order", "order_kind": "REQUERY", "iteration": 2, "turn_issued": 2, "consumed": True,
     "claim_ref": "c", "query": "q", "scope": "", "max_attempts": 3},
]
PROPOSALS = [
    {"next_action": "SEARCH_CODE", "action_parameters": {"query": "order filter"}},
    {"next_action": "GET_CODE", "action_parameters": {"file_path": "svc/new.py"}},
    {"next_action": "VERIFY_HYPOTHESIS", "hypotheses_evaluated": [{"statement": "implemented in svc/a.py"}]},
]
HISTORIES = [
    [
        {"action": "SEARCH_CODE", "evidence_added": 0, "parameters": {"query": "q"}},
        {"action": "SEARCH_CODE", "evidence_added": 0, "parameters": {"query": "q"}},
    ],
    [
        {"action": "GET_CODE", "evidence_added": 1, "parameters": {"file_path": "svc/a.py"}},
        {"action": "SEARCH_LOGS", "evidence_added": 0, "parameters": {"query": "e"}},
        {"action": "SEARCH_CODE", "evidence_added": 0, "parameters": {"query": "q"}},
    ],
    [
        {"action": "SEARCH_CODE", "evidence_added": 0, "parameters": {"query": "q"}},
        {"action": "SEARCH_CODE", "evidence_added": 0, "parameters": {"query": "q"}},
        {"action": "GET_CODE", "evidence_added": 1, "parameters": {"file_path": "svc/a.py"}},
    ],
]


def _run(history, pin, proposal, banked: int = 2):  # type: ignore[no-untyped-def]
    state = PostReasonCascadeState(
        reason_data=dict(proposal), action_history=copy.deepcopy(history), evidence_banked_total=banked,
        triage_route="BUG", description="where is the order filter implemented", knowledge_seed={},
        max_iterations=10, iteration=4,
        prev_focus={"surface": "svc/old.py", "surface_kind": "file", "line_start": None, "line_end": None},
        is_defer=False, d6_fired=False, absence_forced=False, counts={}, formulated_count=1,
        tenant_id="t", investigation_id="i", loop_version=pin,
    )
    out = apply_post_reason_cascade(state)
    return out.reason_data, dict(out.counts), out.prev_focus


def _carrier_sensitive(pin: str) -> int:
    rnd, diffs = random.Random(7), 0
    for base in HISTORIES:
        for proposal in PROPOSALS:
            reference = _run(base, pin, proposal)
            for _ in range(60):
                history = list(base)
                for carrier in rnd.sample(INERT, rnd.randint(1, 2)):
                    history.insert(rnd.randint(0, len(history)), copy.deepcopy(carrier))
                diffs += _run(history, pin, proposal) != reference
    return diffs


def test_inert_carriers_never_change_a_decision_from_lv6() -> None:
    assert _carrier_sensitive("lv7") == 0
    assert _carrier_sensitive("lv6") == 0


def test_the_property_can_fail_at_lv5() -> None:
    assert _carrier_sensitive("lv5") > 0


def test_live_carriers_dispatch_wherever_they_sit() -> None:
    from investigation_agent_platform.application.worker.workflows.forcing import _carry_gap_orders

    for pos in (0, 1, 2):
        carrier = _carry_gap_orders(
            {"claim_gaps": [{"gap_id": "g1", "kind": "DEFINITION", "target": {"file_path": "svc/gap.py"}}]}, 2
        )
        assert carrier is not None
        base = [
            {"action": "GET_CODE", "evidence_added": 1, "parameters": {"file_path": "svc/a.py", "start_line": 1, "end_line": 5}},
            {"action": "SEARCH_CODE", "evidence_added": 0, "parameters": {"query": "q"}},
        ]
        history = list(base)
        history.insert(pos, carrier)
        out = _run(history, "lv7", {"next_action": "FORMULATE_HYPOTHESIS"}, banked=9)[0]
        assert out.get("forced_smallest_fetch") is True, pos


def test_turn_view_drops_non_dicts_and_malformed_carriers() -> None:
    from investigation_agent_platform.application.worker.workflows.forcing import turn_view

    assert turn_view([None, 5, "x", {"action": "A"}], "lv7") == [{"action": "A"}]
    assert turn_view([{"kind": "work_order", "consumed": False}], "lv7") == []
    assert turn_view([{"action": "A"}], "lv5") == [{"action": "A"}]
