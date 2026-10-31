"""Cascade scenario catalog (F10-B tier 1): scripted multi-turn histories.

Each scenario is cascade-focused (the risk lives in the cascade, not in
signal handling). Transcripts in transcripts/ pin the dispatch outputs.
Synthetic data only — no analyzed-repository names.
"""

from __future__ import annotations

SCENARIOS: dict[str, list[dict]] = {
    "pivot_then_formulate": [
        {
            "iteration": 0,
            "reason_data": {
                "next_action": "GET_CODE",
                "action_parameters": {"file_path": "svc/a.py", "start_line": 1, "end_line": 20},
            },
            "action_history": [],
            "evidence_banked_total": 0,
        },
        {
            "iteration": 1,
            "reason_data": {"next_action": "FORMULATE_HYPOTHESIS"},
            "action_history": [],
            "evidence_banked_total": 3,
        },
    ],
    "requery_carrier_beats_gap": [
        {
            "iteration": 2,
            "reason_data": {"next_action": "FORMULATE_HYPOTHESIS"},
            "action_history": [
                {
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
                },
            ],
            "evidence_banked_total": 9,
        },
    ],
    "defer_passthrough": [
        {
            "iteration": 0,
            "reason_data": {"next_action": "GET_CODE", "action_parameters": {"file_path": "svc/b.py"}},
            "action_history": [],
            "evidence_banked_total": 0,
            "is_defer": True,
        },
    ],
    "d6_abstain": [
        {
            "iteration": 0,
            "reason_data": {"next_action": "CONCLUDE", "forced_not_checkable": True},
            "action_history": [],
            "evidence_banked_total": 0,
            "d6_fired": True,
        },
    ],
    "absence_disconfirm": [
        {
            "iteration": 1,
            "reason_data": {
                "next_action": "GET_CODE",
                "action_parameters": {"file_path": "svc/c.py"},
                "forced_absence_disconfirmation": True,
            },
            "action_history": [],
            "evidence_banked_total": 2,
            "absence_forced": True,
        },
    ],
}
