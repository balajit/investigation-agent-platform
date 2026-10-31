"""F8-D16 golden corpus pools and runner (shared by recorder and test)."""

from __future__ import annotations

import copy
import itertools
import random

PROPOSALS: list[dict] = [
    {"next_action": "GET_CODE", "action_parameters": {"file_path": "svc/new.py"}},
    {"next_action": "GET_CODE", "action_parameters": {"file_path": "svc/old.py"}},
    {"next_action": "GET_CODE", "action_parameters": {"file_path": "svc/a.py", "start_line": 1, "end_line": 20}},
    {"next_action": "GET_CODE", "action_parameters": {"file_path": "svc/a.py", "start_line": 10, "end_line": 30}},
    {"next_action": "SEARCH_CODE", "action_parameters": {"query": "order filter"}},
    {"next_action": "SEARCH_CODE", "action_parameters": {}},
    {"next_action": "FIND_SYMBOL", "action_parameters": {"symbol_name": "computeTotal"}},
    {"next_action": "FORMULATE_HYPOTHESIS"},
    {"next_action": "VERIFY_HYPOTHESIS"},
    {"next_action": "CONCLUDE", "action_parameters": {}},
    {"next_action": "GET_CODE", "action_parameters": {"file_path": "svc/gap.py"}},
    {"next_action": "GET_CODE", "action_parameters": {"file_path": "svc/deep.py", "start_line": 100, "end_line": 200}},
    {"next_action": "SEARCH_LOGS", "action_parameters": {"query": "error"}},
    {"next_action": "GET_CODE", "action_parameters": {"file_path": "svc/new.py", "start_line": 5, "end_line": 15}},
    # formulate: unevaluated hyps, gathering proposal on a known surface (no pivot)
    {
        "next_action": "GET_CODE",
        "action_parameters": {"file_path": "svc/old.py"},
        "hypotheses_evaluated": [{"statement": "filter parses the query"}],
    },
    # reread: VERIFY over definition language (floor stands down once banked)
    {
        "next_action": "VERIFY_HYPOTHESIS",
        "hypotheses_evaluated": [{"statement": "implemented in svc/a.py"}],
    },
    # callsite: CALL_EDGE claim naming an unread caller
    {
        "next_action": "FORMULATE_HYPOTHESIS",
        "claims": [{"claim_kind": "CALL_EDGE", "target": {"file_path": "svc/caller.py"}}],
    },
]


def _evidenced_get(path: str, start: int | None = None, end: int | None = None) -> dict:
    params: dict = {"file_path": path}
    if start is not None:
        params["start_line"] = start
    if end is not None:
        params["end_line"] = end
    return {"action": "GET_CODE", "evidence_added": 1, "parameters": params}


def _requery_carrier(**over) -> dict:  # type: ignore[no-untyped-def]
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
            "action_type": "GET_CODE",
            "action_parameters": {"file_path": "svc/req.py"},
            "addresses": {"triage_route": "BUG", "claim_kind": "untyped"},
            "cost_estimate": 1,
            "seed_source": "test:carrier",
            "status": "PENDING",
        },
    }
    base.update(over)
    return base


def _gap_feed(*gaps: dict, iteration: int = 2) -> dict:
    return {"kind": "claim_gap_feed", "iteration": iteration, "gaps": list(gaps)}


def _gap(gap_id: str = "g1", kind: str = "DEFINITION", target: dict | None = None) -> dict:
    return {
        "gap_id": gap_id,
        "kind": kind,
        "claim_ref": "ck1",
        "target": target if target is not None else {"file_path": "svc/gap.py"},
    }


def _live_gap_carrier(*gaps: dict, iteration: int = 2) -> dict:
    from investigation_agent_platform.application.worker.workflows.forcing import (
        _carry_gap_orders,
    )

    carrier = _carry_gap_orders({"claim_gaps": list(gaps)}, iteration)
    assert carrier is not None
    return carrier


HISTORIES: list[list[dict]] = [
    [],
    [_evidenced_get("svc/old.py")],
    [_evidenced_get("svc/old.py", 1, 20), _evidenced_get("svc/other.py", 1, 10)],
    [_requery_carrier()],
    [_requery_carrier(consumed=True)],
    [_gap_feed(_gap())],
    [_gap_feed(_gap("g2", "HISTORY", {"file_path": "svc/h.py"}))],
    [_requery_carrier(), _gap_feed(_gap())],
    [{"kind": "ledger_requery_directive", "query": "stale", "action_type": "GET_CODE"}],
    [{"kind": "work_order", "order_kind": "BOGUS", "consumed": False}],
    [_evidenced_get("svc/new.py"), _requery_carrier()],
    [_gap_feed(_gap("g9", "CAUSAL", {"file_path": "svc/c.py"})), _evidenced_get("svc/c.py")],
    [
        {"action": "GET_CODE", "evidence_added": 5, "parameters": {"file_path": "svc/a.py"}},
        {"action": "SEARCH_CODE", "evidence_added": 4, "parameters": {"query": "q"}},
    ],
    [
        {"action": "SEARCH_LOGS", "evidence_added": 0, "parameters": {"query": "error"}},
        {"action": "SEARCH_LOGS", "evidence_added": 0, "parameters": {"query": "error"}},
    ],
    [_live_gap_carrier(_gap("g1", "DEFINITION", {"file_path": "svc/gap.py"}))],
    [_live_gap_carrier(_gap("g2", "HISTORY", {"file_path": "svc/h.py"}))],
    [
        _requery_carrier(),
        _live_gap_carrier(_gap("g3", "DEFINITION", {"file_path": "svc/gap.py"})),
    ],
]

PRE = [(False, False, False), (True, False, False), (False, True, False), (False, False, True), (True, True, True)]
PINS = ["lv5", "lv6", "lv7"]
BANKED = [0, 2, 9]
ITERS = [0, 1, 3, 8]
SEEDS: list[dict] = [
    {},
    {"frames": [{"file": "svc/a.py", "line": 10}, {"file": "svc/b.py", "line": 5}]},
]


def corpus(n: int = 600, seed: int = 20261031) -> list[tuple]:
    space = list(
        itertools.product(
            range(len(PROPOSALS)), range(len(HISTORIES)), PRE, PINS, range(3), range(4), range(len(SEEDS))
        )
    )
    sampled = random.Random(seed).sample(space, min(n, len(space)))
    # Stratified top-up: one known-firing case per thin rule (8th element = formulated_count).
    p_idx = lambda pred: next(i for i, p in enumerate(PROPOSALS) if pred(p))
    h_idx = lambda pred: next(i for i, h in enumerate(HISTORIES) if pred(h))
    _form = p_idx(lambda p: "hypotheses_evaluated" in p and p.get("next_action") == "GET_CODE")
    _verify_def = p_idx(
        lambda p: p.get("next_action") == "VERIFY_HYPOTHESIS"
        and any("implemented in" in str(h.get("statement", "")) for h in (p.get("hypotheses_evaluated") or []))
    )
    _callsite = p_idx(lambda p: "claims" in p)
    _search_err = p_idx(
        lambda p: p.get("next_action") == "SEARCH_LOGS"
        and (p.get("action_parameters") or {}).get("query") == "error"
    )
    _formulate_plain = p_idx(lambda p: p.get("next_action") == "FORMULATE_HYPOTHESIS" and "claims" not in p)
    _get_old = p_idx(
        lambda p: p.get("next_action") == "GET_CODE"
        and (p.get("action_parameters") or {}).get("file_path") == "svc/old.py"
        and "hypotheses_evaluated" not in p
    )
    _empties = h_idx(lambda h: len(h) == 2 and all(x.get("action") == "SEARCH_LOGS" for x in h))
    _live_def = h_idx(
        lambda h: len(h) == 1 and h[0].get("kind") == "work_order_feed" and h[0].get("order_kind") == "GAP_FILL"
    )
    _rich2 = h_idx(lambda h: len(h) == 2 and {x.get("action") for x in h} == {"GET_CODE", "SEARCH_CODE"})
    targeted = [
        (_form, 0, PRE[0], "lv7", 1, 2, 0, 0),
        (_form, 0, PRE[0], "lv5", 1, 2, 0, 0),
        (_form, 0, PRE[0], "lv6", 1, 2, 0, 0),
        # reread (formulated=1 so formulate stands down)
        (_verify_def, 0, PRE[0], "lv7", 2, 2, 0, 1),
        (_verify_def, 0, PRE[0], "lv5", 2, 2, 0, 1),
        (_verify_def, 0, PRE[0], "lv6", 2, 2, 0, 1),
        (_verify_def, 0, PRE[0], "lv7", 2, 3, 0, 1),
        # callsite (banked=9 so floor stands down; floor fires below 3 banked)
        (_callsite, 0, PRE[0], "lv7", 2, 2, 0, 0),
        (_callsite, 0, PRE[0], "lv5", 2, 2, 0, 0),
        (_callsite, 0, PRE[0], "lv6", 2, 2, 0, 0),
        (_callsite, 0, PRE[0], "lv7", 2, 3, 0, 0),
        # replan (two empties + same proposal, banked=0)
        (_search_err, _empties, PRE[0], "lv7", 0, 2, 0, 0),
        (_search_err, _empties, PRE[0], "lv5", 0, 2, 0, 0),
        (_search_err, _empties, PRE[0], "lv6", 0, 2, 0, 0),
        (_search_err, _empties, PRE[0], "lv7", 0, 3, 0, 0),
        # smallest_fetch (FORMULATE + live gap carrier)
        (_formulate_plain, _live_def, PRE[0], "lv7", 2, 2, 0, 0),
        (_formulate_plain, _live_def, PRE[0], "lv5", 2, 2, 0, 0),
        (_formulate_plain, _live_def, PRE[0], "lv6", 2, 2, 0, 0),
        (_formulate_plain, _live_def, PRE[0], "lv7", 2, 3, 0, 0),
        # synthesis (rich 2-action banked history, gathering proposal, formulated=0)
        (_get_old, _rich2, PRE[0], "lv7", 2, 3, 0, 0),
    ]
    return sampled + [t for t in targeted if t not in sampled]


def run_case(case: tuple) -> dict:
    from investigation_agent_platform.application.worker.workflows.forcing import (
        PostReasonCascadeState,
        apply_post_reason_cascade,
    )

    p, h, (defer, d6, absence), pin, banked_i, iter_i, seed_i = case[:7]
    formulated = case[7] if len(case) > 7 else banked_i % 2
    history = copy.deepcopy(HISTORIES[h])
    state = PostReasonCascadeState(
        reason_data=copy.deepcopy(PROPOSALS[p]),
        action_history=history,
        evidence_banked_total=BANKED[banked_i],
        triage_route="BUG",
        description="orders fail with an error in the order filter",
        knowledge_seed=copy.deepcopy(SEEDS[seed_i]),
        max_iterations=10,
        iteration=ITERS[iter_i],
        prev_focus={"surface": "svc/old.py", "surface_kind": "file", "line_start": None, "line_end": None},
        is_defer=defer,
        d6_fired=d6,
        absence_forced=absence,
        counts={},
        formulated_count=formulated,
        tenant_id="tenant-a",
        investigation_id="inv-1",
        loop_version=pin,
    )
    out = apply_post_reason_cascade(state)
    return {
        "reason": out.reason_data,
        "counts": dict(out.counts),
        "focus": out.prev_focus,
        "consumed_ref": history.index(out.consumed_order_ref)
        if out.consumed_order_ref in history
        else None,
        "stamped": [i for i, e in enumerate(history) if isinstance(e, dict) and e.get("consumed") is True],
    }
