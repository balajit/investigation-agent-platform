"""Offline replay harness skeleton for RunInvestigationWorkflow (F10-B, Sonnet F D12).

Three tiers:
1. Scripted cascade scenarios (pure, no server): drive apply_post_reason_cascade
   through recorded multi-turn histories and pin dispatch outputs.
2. Minimal workflow replay mechanics: a tiny test workflow proves history
   synthesis + SandboxedWorkflowRunner Replayer + input-parity interception
   work in this repo (negative control included).
3. Full RunInvestigationWorkflow replay: open (needs scripted activities for
   every activity the workflow calls); the mechanics in tier 2 are the
   foundation. Tier 1 is the corpus that proves F8-D15/F9 command-neutral.
"""

from __future__ import annotations

import dataclasses
from typing import Any


def record_call(name: str, arg: Any, result: Any) -> dict[str, Any]:
    """One scripted activity call: (activity, input, result) triple as JSON-safe dict."""
    return {"activity": name, "input": arg, "result": result}


def check_input_parity(recorded: list[dict[str, Any]], replayed: list[dict[str, Any]]) -> list[str]:
    """Field-by-field input comparison the SDK Replayer does not do (F10 D12 step 3)."""
    problems: list[str] = []
    if len(recorded) != len(replayed):
        return [f"call count differs: recorded={len(recorded)} replayed={len(replayed)}"]
    for i, (rec, rep) in enumerate(zip(recorded, replayed)):
        if rec.get("activity") != rep.get("activity"):
            problems.append(f"call {i}: activity {rec.get('activity')} != {rep.get('activity')}")
        if rec.get("input") != rep.get("input"):
            problems.append(f"call {i} ({rec.get('activity')}): input differs")
    return problems


def cascade_scenario_dispatch(
    turns: list[dict[str, Any]], *,
    loop_version: str = "lv8",
) -> list[dict[str, Any]]:
    """Drive the real cascade through scripted turns, return per-turn dispatch pins."""
    from investigation_agent_platform.application.worker.workflows.forcing import (
        PostReasonCascadeState,
        apply_post_reason_cascade,
    )

    out: list[dict[str, Any]] = []
    prev_focus = None
    for turn in turns:
        state = PostReasonCascadeState(
            reason_data=dict(turn.get("reason_data", {})),
            action_history=list(turn.get("action_history", [])),
            evidence_banked_total=int(turn.get("evidence_banked_total", 0)),
            triage_route=str(turn.get("triage_route", "UNKNOWN")),
            description=str(turn.get("description", "replay scenario")),
            knowledge_seed={},
            max_iterations=10,
            iteration=int(turn.get("iteration", 0)),
            prev_focus=prev_focus,
            is_defer=bool(turn.get("is_defer", False)),
            d6_fired=bool(turn.get("d6_fired", False)),
            absence_forced=bool(turn.get("absence_forced", False)),
            counts=dict(turn.get("counts", {})),
            formulated_count=int(turn.get("formulated_count", 0)),
            tenant_id="replay-tenant",
            investigation_id="replay-inv",
            loop_version=loop_version,
        )
        res = apply_post_reason_cascade(state)
        rd = dict(res.reason_data or {})
        out.append(
            {
                "next_action": rd.get("next_action"),
                "forced": sorted(k for k in rd if str(k).startswith("forced_") and rd.get(k)),
                "prev_focus": res.prev_focus,
                "consumed_order_ref": res.consumed_order_ref,
            }
        )
        prev_focus = res.prev_focus
    return out


def dataclass_roundtrip_ok(state: Any) -> bool:
    """PostReasonCascadeState survives a dataclasses round-trip (replay needs serializable state)."""
    try:
        _ = dataclasses.replace(state)
        return True
    except Exception:
        return False
