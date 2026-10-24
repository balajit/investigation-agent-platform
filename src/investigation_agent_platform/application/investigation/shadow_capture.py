# src/investigation_agent_platform/application/investigation/shadow_capture.py
"""G6-measure shadow capture (pure; compute-only, never acts).

Builds the frozen ``PlannerSnapshot`` for one loop turn from loop-local
state and replays it through the shadow policy, returning JSON-scalar
records for the operator ledger. The workflow calls this beside the
forcing cascade (never inside it): the returned record never routes
acquisition, never mutates loop state, never touches I/O or the clock.

Testing boundary: everything here is pure and unit-tested. The seven
line owed-flag wiring (which ``_should_force_*`` helper feeds which
owed flag, always with the cap bypassed via count=0) lives in
``application/worker/workflows.py`` and is review-verified; the helpers
themselves are deterministic and already covered. No behavior change is
possible from this module: it allocates new objects only.
"""

from __future__ import annotations

from typing import Any

from investigation_agent_platform.application.investigation.action_policy import (
    HistoryEntry,
    PlannerSnapshot,
)
from investigation_agent_platform.application.investigation.replay import (
    RecordedStep,
    replay_history,
    report_to_json_dict,
)

_VERIFIED_MARKERS = ("VERIFIED",)
_SETTLED_MARKERS = ("REJECTED", "CONTRADICTED", "FAILED", "REFUTED")


def _status_upper(entry: Any) -> str:
    if isinstance(entry, dict):
        raw = entry.get("status", "")
    else:
        raw = getattr(entry, "status", "")
    if isinstance(raw, dict):
        raw = raw.get("value", "")
    return str(raw or "").strip().upper()


def verification_state(hypotheses_evaluated: Any) -> tuple[int, bool]:
    """Split evaluated hypotheses into (unverified_count, verified_present).

    Documented interpretation (the loop keeps no native verified/
    unverified counters): an entry is verified when its status names
    VERIFIED (excluding UNVERIFIED); settled when rejected/contradicted/
    failed/refuted; everything else — including a missing status —
    awaits evaluation and counts as unverified. Any systematic
    mis-mapping surfaces as divergence in measurement, which is itself
    signal; the mapping lives here, in one place, to fix.
    """
    unverified = 0
    verified_present = False
    entries = hypotheses_evaluated if isinstance(hypotheses_evaluated, list) else []
    for entry in entries:
        upper = _status_upper(entry)
        if not upper:
            unverified += 1
            continue
        if "UNVERIFIED" in upper:
            unverified += 1
            continue
        if "VERIFIED" in upper:
            verified_present = True
            continue
        if any(m in upper for m in _SETTLED_MARKERS):
            continue
        unverified += 1
    return unverified, verified_present


def history_entries(action_history: Any) -> list[HistoryEntry]:
    """Map prior loop turns to shadow history (outcome-tolerating).

    Entries are pre-execution turns already enriched post-execution, so
    ``outcome_status`` is present; a missing status defaults to
    SUCCEEDED (the common case). Unknown actions/outcomes pass through —
    the policy tolerates and counts them, never fails on them.
    """
    out: list[HistoryEntry] = []
    if not isinstance(action_history, list):
        return out
    for turn in action_history:
        if not isinstance(turn, dict):
            continue
        action = str(turn.get("action", "") or "")
        if not action:
            continue
        outcome = str(turn.get("outcome_status") or "SUCCEEDED")
        try:
            out.append(HistoryEntry(action=action[:64], outcome=outcome[:32]))
        except Exception:
            continue
    return out


def build_shadow_snapshot(
    *,
    iteration: int,
    max_iterations: int,
    actions_used: int,
    action_limit: int,
    tokens_used: int,
    token_limit: int,
    evidence_banked_total: int,
    formulated_count: int,
    unverified_count: int = 0,
    verified_present: bool = False,
    reasoning_available: bool = True,
    value_target_present: bool = False,
    stack_walk_owed: bool = False,
    below_evidence_floor: bool = False,
    formulate_owed: bool = False,
    synthesis_owed: bool = False,
    reread_path_present: bool = False,
    callsite_owed: bool = False,
    forcing_used: dict[str, int] | None = None,
    action_history: list[dict[str, Any]] | None = None,
) -> PlannerSnapshot:
    """Assemble a frozen snapshot from loop-local scalars (pure).

    Budget mapping (documented): the loop caps actions via
    ``max_iterations`` (one dispatch per turn) and tokens via the
    activity-enforced cap the workflow never sees — hence
    ``token_limit=0`` (unbounded) unless a cap is threaded through
    later.     Budget mapping (documented): the loop caps actions via
    ``max_iterations`` (one dispatch per turn) and tokens via the
    activity-enforced cap the workflow never sees — hence
    ``token_limit=0`` (unbounded) unless a cap is threaded through
    later. ``formulate``/``synthesis`` share the loop's combined cap of
    2: callers mirror the shared counter on both rules so the shadow
    withholds where the loop would stop firing. Used counts pass
    through verbatim (unknown rule names included — the policy reads
    only known rules, and the ledger must preserve what the loop
    reported); negative and non-integer counts are dropped.
    """
    used: dict[str, int] = {}
    for key, value in (forcing_used or {}).items():
        if isinstance(value, bool):
            continue
        try:
            ivalue = int(value)
        except (TypeError, ValueError):
            continue
        if ivalue >= 0:
            used[str(key)] = ivalue
    return PlannerSnapshot(
        iteration=iteration,
        max_iterations=max_iterations,
        actions_used=actions_used,
        action_limit=action_limit,
        tokens_used=tokens_used,
        token_limit=token_limit,
        evidence_banked_total=evidence_banked_total,
        formulated_count=formulated_count,
        unverified_count=unverified_count,
        verified_present=verified_present,
        reasoning_available=reasoning_available,
        value_target_present=value_target_present,
        stack_walk_owed=stack_walk_owed,
        below_evidence_floor=below_evidence_floor,
        formulate_owed=formulate_owed,
        synthesis_owed=synthesis_owed,
        reread_path_present=reread_path_present,
        callsite_owed=callsite_owed,
        history=history_entries(action_history),
        forcing_used=used,
    )


def capture_turn(
    snapshot: PlannerSnapshot,
    recorded_action: str,
    recorded_outcome: str = "RECORDED",
) -> dict[str, Any]:
    """Replay one turn through the shadow policy; return JSON scalars.

    Reuses the unit-harness comparator, so live measurement and fixture
    measurement share one divergence vocabulary. Never raises on
    well-formed input; malformed snapshots fail closed in
    ``PlannerSnapshot`` construction (caller-side), never here.
    """
    report = replay_history(
        [
            RecordedStep(
                snapshot=snapshot,
                recorded_action=recorded_action,
                recorded_outcome=recorded_outcome,
            )
        ]
    )
    step = report_to_json_dict(report)["steps"][0]
    step["snapshot"] = snapshot.model_dump(mode="json")
    return step


def summarize_trace(trace: list[dict[str, Any]]) -> dict[str, int]:
    """Aggregate a shadow trace with eligibility semantics (§4.2).

    Turns carrying ``capture_error`` are capture-health signal, not
    comparable turns: they count toward ``turns`` and ``capture_errors``
    but are excluded from the eligible denominator ``T`` (``eligible``)
    and therefore never inflate ``diverged``. ``matched`` counts
    ``match is True``; ``diverged = eligible - matched``.
    """
    turns = len(trace)
    errors = 0
    matched = 0
    for turn in trace:
        if not isinstance(turn, dict) or turn.get("capture_error"):
            errors += 1
            continue
        if turn.get("match") is True:
            matched += 1
    eligible = turns - errors
    return {
        "turns": turns,
        "matched": matched,
        "capture_errors": errors,
        "eligible": eligible,
        "diverged": eligible - matched,
    }
