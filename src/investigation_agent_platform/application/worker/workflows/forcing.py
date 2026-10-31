# src/investigation_agent_platform/application/worker/workflows/forcing.py
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from temporalio import workflow

from investigation_agent_platform.application.worker.reasoning_failure import (
    TRANSIENT_FAILURE_TYPE,
)

logger = logging.getLogger(__name__)

with workflow.unsafe.imports_passed_through():
    pass

with workflow.unsafe.imports_passed_through():
    pass

with workflow.unsafe.imports_passed_through():
    # G6-measure shadow capture: pure compute-and-log helpers (no I/O,
    # no clock, no execution). Imported for the per-turn shadow replay;
    # the workflow never branches on its output.

    # D3 Phase 0 shadow definitions: pure receipt pairing + summary
    # (canonical digests only — deterministic, replay-safe). The
    # workflow never branches on the paired output.
    pass

with workflow.unsafe.imports_passed_through():
    # G3: ledger directive helpers — pure, deterministic, replay-safe
    # (hashlib/re only at call time; no I/O, no clock, no process-local
    # ledger reads). Imported passed-through like the shadow helpers
    # because the application layer is not import-safe in the sandbox.
    from investigation_agent_platform.application.evidence.retrieval_ledger import (
        RETRYABLE_ACTIONS,
        WP_A3_DEFAULT_MAX_ATTEMPTS,
        _normalize_scope,
        directive_consumed,
        recorded_attempt_summaries,
    )

with workflow.unsafe.imports_passed_through():
    from investigation_agent_platform.domain.investigation.loop_version import (
        LOOP_VERSION_CURRENT,
        LOOP_VERSION_LV4,
        LOOP_VERSION_LV6,
        loop_version_at_least,
    )

with workflow.unsafe.imports_passed_through():
    from investigation_agent_platform.domain.collection.focus import (
        FocusPoint,
        FocusShift,
        detect_shift,
        focus_from_reason_data,
        pivot_can_pay_off,
    )
    from investigation_agent_platform.domain.collection.frontier import (
        EvidenceState as FrontierEvidenceState,
    )
    from investigation_agent_platform.domain.collection.frontier import (
        order_candidates,
    )
    from investigation_agent_platform.domain.collection.work_orders import (
        MAX_WORK_ORDER_SEEDS,
        WORK_ORDER_VERSION_CURRENT,
        FailureCharacter,
        WorkOrder,
        WorkOrderKind,
        WorkOrderStatus,
        work_orders_to_seeds,
    )


def _reason_summary(reason_data: dict[str, Any]) -> str:
    """One-line summary of a reason-activity decision for timeline descriptions.

    Pure function (deterministic, no I/O) so the per-iteration trace recorded
    on the timeline is unit-testable without a Temporal test environment.
    Includes a compact digest of the attempted query (keywords/services/
    template) so a later reader can see WHAT was tried, not just the action.
    """
    params = reason_data.get("action_parameters") or {}
    query_bits: list[str] = []
    if isinstance(params, dict):
        for key in ("keywords", "services", "template_id", "file_path"):
            value = params.get(key)
            if value:
                query_bits.append(f"{key}={str(value)[:80]}")
    query = f" query=[{'; '.join(query_bits)}]" if query_bits else ""
    # C-1: post-execution trace — forced overrides and outcome ride the
    # same one-liner once the history entry is enriched (the call site
    # merges the enriched entry over the raw decision). Absent keys render
    # as-is (None), so pre-execution summaries keep their old shape.
    forced_bits: list[str] = []
    if isinstance(reason_data, dict):
        for key in (
            "forced_formulate",
            "forced_reread",
            "forced_log_search",
            "forced_walk_stack",
            "forced_replan",
        ):
            if reason_data.get(key):
                forced_bits.append(key.removeprefix("forced_"))
    forced = f" forced=[{','.join(forced_bits)}]" if forced_bits else ""
    outcome_bits: list[str] = []
    if isinstance(reason_data, dict):
        for key in ("outcome_status", "evidence_added", "miss_reason"):
            value = reason_data.get(key)
            if value is not None and value != "":
                outcome_bits.append(f"{key}={value}")
    outcome = f" outcome=[{'; '.join(outcome_bits)}]" if outcome_bits else ""
    # D3: triage route rides the trace when the enriched entry carries it.
    triage = ""
    if isinstance(reason_data, dict):
        route = str(reason_data.get("triage_route", "") or "")
        if route and route != "UNKNOWN":
            triage = f" triage={route}"
    # C-1: the enriched entry carries the DISPATCHED action (post-override);
    # the raw decision carries the PROPOSED one. Prefer dispatched so forced
    # overrides read correctly (action=FORMULATE_HYPOTHESIS forced=[formulate]).
    action = "?"
    _hyps_raw: Any = None
    _obs_raw: Any = None
    if isinstance(reason_data, dict):
        action = str(reason_data.get("action", reason_data.get("next_action", "?")) or "?")
        _hyps_raw = reason_data.get("hypotheses_evaluated")
        _obs_raw = reason_data.get("observations")
    # C-1: merged history entries carry INT counts where decisions carry
    # lists — tolerate both (an int shadowing the list made len() raise
    # TypeError and failed the run).
    hyp_count = _hyps_raw if isinstance(_hyps_raw, int) else len(_hyps_raw or [])
    obs_count = _obs_raw if isinstance(_obs_raw, int) else len(_obs_raw or [])
    return (
        f"reason: action={action} "
        f"readiness={reason_data.get('conclusion_readiness', '?')} "
        f"hypotheses={hyp_count} "
        f"observations={obs_count}"
        f"{query}"
        f"{forced}"
        f"{outcome}"
        f"{triage}"
    )


def _resolve_max_iterations(requested: int | None) -> int:
    """Effective iteration cap: profile-resolved input wins, clamped to sane bounds.

    Pure function so budget resolution is unit-testable without Temporal.
    The clamp bounds runaway profiles (cost control); the floor keeps the
    loop meaningful. Deterministic — safe to call inside the workflow.
    """
    WORKFLOW_LOCAL_ITERATION_GUARD = 10
    if requested is None:
        return WORKFLOW_LOCAL_ITERATION_GUARD
    return max(1, min(int(requested), 50))


def _should_force_formulate(
    iteration: int,
    formulated_count: int,
    hypotheses_evaluated: list | None,
    evidence_banked_total: int,
    forced_count: int,
) -> bool:
    """G8 synthesis-forcing rule: commit thinking before gathering more.

    Fires when the model holds unevaluated hypotheses (non-empty
    hypotheses_evaluated) yet never FORMULATEd, evidence already banked,
    and at least two gathering turns elapsed — the loop then overrides the
    proposed action with FORMULATE_HYPOTHESIS carrying the same content.
    Bounded (max 2 forced attempts per run) so a model with genuinely
    nothing to commit cannot be wedged; empty hypotheses never force
    (nothing to commit). Pure and deterministic — workflow-safe.
    """
    try:
        pending = list(hypotheses_evaluated or [])
    except Exception:
        return False
    if not pending or formulated_count > 0 or evidence_banked_total <= 0:
        return False
    return not (int(iteration or 0) < 2 or int(forced_count or 0) >= 2)


# P0-2 (G3 structural twin): definition-strength language mirrored from
# VerificationEngine.DEFINITION_PATTERNS (verification.py is the source of
# truth; duplicated here because the application layer is not import-safe
# inside the Temporal workflow sandbox). Range-read operations mirrored
# from VerificationEngine.GET_OPERATIONS for the same reason.
_REREAD_DEFINITION_PATTERNS = (
    "defined in",
    "implemented in",
    "declared in",
    "definition is in",
)
_REREAD_GET_OPERATIONS = frozenset({"GET_SOURCE", "GET_CODE", "READ_FILE"})
_REREAD_PATH_TOKEN_RE = r"[A-Za-z0-9_.\-~/][A-Za-z0-9_.\-/]*\.[A-Za-z0-9]{1,10}"


# B-4: error/log language in the complaint means the first gathering turn
# belongs to SEARCH_LOGS (the L6 run banked SOURCE_CODE for a service-error
# question because the model chose SEARCH_CODE; the backend had 356 error
# docs). GET_CODE proposals are exempt (G2 explicit-path wins); downstream
# actions (FORMULATE/VERIFY/CONCLUDE/PROBE_API) are never overridden.
_LOG_FORCE_ERROR_PATTERNS = (
    "error",
    "errors",
    "failing",
    "failed",
    "failure",
    "exception",
    "traceback",
    "timed out",
    "timeout",
    " 500",
    " 503",
    " 429",
    " 409",
    " 502",
    " 504",
    "logs",
    "log lines",
)
_LOG_FORCE_FROM_ACTIONS = frozenset(
    {"SEARCH_CODE", "QUERY_STATE", "CORRELATE", "FIND_SYMBOL", "FIND_CALL_GRAPH"}
)
_LOG_FORCE_STOPWORDS = frozenset(
    {
        "what",
        "where",
        "when",
        "which",
        "does",
        "doing",
        "have",
        "has",
        "had",
        "with",
        "from",
        "that",
        "this",
        "these",
        "those",
        "there",
        "their",
        "about",
        "show",
        "recent",
        "service",
        "services",
        "investigation",
        "platform",
        "application",
        "src",
    }
)


# S3: whole-word trigger patterns. The complaint is matched with
# alphanumeric boundaries so code identifiers never trip the log force:
# "ErrorConstants" must not match "error", "catalogs" must not match
# "logs". Multi-word patterns ("log lines", "timed out") and numeric
# codes ("500") work unchanged under the same boundaries.
_LOG_FORCE_ERROR_RES = tuple(
    re.compile(r"(?<![a-z0-9])" + re.escape(pattern.strip().lower()) + r"(?![a-z0-9])")
    for pattern in _LOG_FORCE_ERROR_PATTERNS
)

# S3: a forced SEARCH_LOGS that the tenant could not execute must never
# be forced again in the same run. The execute activity records the
# denial on the history entry (outcome_status BLOCKED, miss_reason
# authorization_denied); the force rule reads only that record — pure.
_LOG_FORCE_DENIED_MISS_REASONS = frozenset({"authorization_denied"})
_LOG_FORCE_DENIED_STATUSES = frozenset({"BLOCKED"})


# D2: intake frames force a stack walk before any other gathering. An
# explicit frame list is the strongest first-turn signal (stronger than
# log-first or synthesis forcing): the fault location is already named.
_WALK_STACK_CAP_ACTIONS = frozenset(
    {
        "QUERY_STATE",
        "SEARCH_LOGS",
        "GET_CODE",
        "SEARCH_CODE",
        "FIND_SYMBOL",
        "FIND_CALL_GRAPH",
        "GET_CODE_HISTORY",
        "PROBE_API",
        "CORRELATE",
    }
)


def dispatched_parameters(reason_data: dict[str, Any] | None) -> dict[str, Any]:
    """Parameters of the action the cascade actually dispatched (pure).

    History must record what was dispatched, not what the model proposed:
    forced overrides (pivot, walk-stack, requery, floor, ...) rewrite
    ``action_parameters`` on the post-cascade ``reason_data``. Reading the
    pre-cascade proposal made forced reads invisible to dedupe/damping and
    let never-fetched proposals count as collected. Never raises.
    """
    try:
        params = (reason_data or {}).get("action_parameters")
        return dict(params) if isinstance(params, dict) else {}
    except Exception:
        return {}


#: History entries that are work-order carriers, not turns. The workflow appends
#: them beside the turn entries (a REQUERY order and a GAP_FILL feed); a real turn
#: entry has no ``kind`` key (a unit test pins that).
CARRIER_KINDS = frozenset({"work_order", "work_order_feed"})


def is_carrier_entry(entry: Any) -> bool:
    """Whether a history entry is a work-order carrier (pure, never raises)."""
    try:
        return isinstance(entry, dict) and entry.get("kind") in CARRIER_KINDS
    except Exception:
        return False


def turn_entries(history: Any) -> list[dict[str, Any]]:
    """The real turn entries of an action history, carriers removed (pure).

    Order preserved; non-dict items are not turns and are dropped. The input is
    never mutated. Positional readers ("the last N turns") use this view so a
    carrier cannot displace a turn (review F2); carrier readers index into the
    full list and must not.
    """
    try:
        return [e for e in (history or []) if isinstance(e, dict) and not is_carrier_entry(e)]
    except Exception:
        return []


def _carriers_separated(loop_version: Any) -> bool:
    """Whether the run's pin reads turns apart from carriers (lv6+), fail-safe.

    An unrecognized or unreadable pin returns False (keep the old behavior).
    """
    try:
        return bool(loop_version_at_least({"loop_version": loop_version}, LOOP_VERSION_LV6))
    except Exception:
        return False


def _should_force_walk_stack(
    intake_seed: dict | None,
    reason_data: dict | None,
    action_history: list | None,
    forced_count: int,
) -> dict | None:
    """WALK_STACK parameters to force, or None.

    Fires when the intake seed carries stack frames, the model proposes a
    different gathering action, and no WALK_STACK has banked evidence yet.
    Bounded (max 2 per run; practically once). Pure — workflow-safe.
    """
    try:
        data = reason_data or {}
        if str(data.get("next_action", "")).upper() not in _WALK_STACK_CAP_ACTIONS:
            return None
        if int(forced_count or 0) >= 2:
            return None
        seed = intake_seed or {}
        frames = [f for f in (seed.get("frames") or []) if isinstance(f, dict)]
        if not frames:
            return None
        for entry in action_history or []:
            try:
                action = str((entry or {}).get("action", "")).upper()
                evidenced = int((entry or {}).get("evidence_added", 0) or 0)
            except (TypeError, ValueError):
                continue
            if action == "WALK_STACK" and evidenced > 0:
                return None
        params: dict[str, Any] = {"frames": frames[:10]}
        error_class = str(seed.get("error_class", "") or "").strip()
        if error_class:
            params["error_class"] = error_class[:256]
        return params
    except Exception:
        return None


def _should_force_pivot_recollect(
    prev_focus: dict[str, Any] | None,
    reason_data: dict[str, Any] | None,
    turns_remaining: int,
    recent_targets: list[str] | None,
) -> dict[str, Any] | None:
    """Collection-forcing override on focus pivot (P6 2b-i, pure).

    After reasoning, if the point of focus changed surfaces and the loop
    has turns left for the recollect to pay off, force a collection fetch
    on the new focus instead of verifying or concluding on the stale one.
    File foci read the new file (pivots land on code); bare-symbol foci
    resolve the symbol first. Damping: a surface collected in recent
    turns never refires, so the rule cannot ping-pong.

    Budget-blind by design (agentic layers own budget governance); the
    only gate is liveness — turns_remaining covers the collection turn
    plus at least one turn to use what it banks. Never raises.
    """
    try:
        if not pivot_can_pay_off(turns_remaining):
            return None
        prev: FocusPoint | None = None
        if isinstance(prev_focus, dict):
            try:
                prev = FocusPoint.model_validate(prev_focus)
            except Exception:
                prev = None
        curr = focus_from_reason_data(reason_data if isinstance(reason_data, dict) else {})
        if curr is None or detect_shift(prev, curr) != FocusShift.PIVOTED:
            return None
        recent = {str(r or "") for r in (recent_targets or []) if str(r or "")}
        if f"{curr.surface_kind}:{curr.surface}" in recent:
            return None
        if curr.surface_kind == "file":
            params: dict[str, Any] = {"file_path": curr.surface}
            if curr.line_start is not None and curr.line_start >= 1:
                params["start_line"] = curr.line_start
            if curr.line_end is not None and curr.line_end >= 1:
                params["end_line"] = curr.line_end
            params["revision"] = "HEAD"
            return {
                "next_action": "GET_CODE",
                "action_parameters": params,
                "forced_pivot_recollect": True,
            }
        return {
            "next_action": "FIND_SYMBOL",
            "action_parameters": {"symbol_name": curr.surface, "revision": "HEAD"},
            "forced_pivot_recollect": True,
        }
    except Exception:
        return None


def _carry_requery_order(
    verify_data: dict[str, Any] | None, iteration: int
) -> dict[str, Any] | None:
    """G3 order carrier to append to action_history, or None (2b step 5).

    Replaces the ``ledger_requery_directive`` marker dict: validates the
    verify result's ``ledger_requery`` dict minimally (non-empty query,
    known retryable action type) and returns ONE new history entry whose
    ``order`` payload is a PENDING REQUERY WorkOrder. The G3 cascade
    slot dispatches from carried orders newest-first; the workflow
    stamps the dispatched carrier consumed in place, exactly as it did
    markers. Strictly additive — None for anything unexpected, so a
    malformed directive never breaks the loop here. Pure.

    The carrier keeps the flat claim accounting fields
    (claim_ref/query/scope/turn_issued/max_attempts) beside the order
    payload so attempt-budget matching (``directive_consumed``) works
    unchanged. Order IDs are deterministic per turn so replays rebuild
    identical carriers.
    """
    try:
        data = verify_data if isinstance(verify_data, dict) else {}
        directive = data.get("ledger_requery")
        if not isinstance(directive, dict):
            return None
        query = str(directive.get("query") or "").strip()
        if not query:
            return None
        action_type = str(directive.get("action_type") or "").upper()
        if action_type not in RETRYABLE_ACTIONS:
            return None
        params = _flat_scalar_params(directive.get("action_parameters"))
        if params is None:
            return None
        try:
            max_attempts = max(1, int(directive.get("max_attempts", WP_A3_DEFAULT_MAX_ATTEMPTS)))
        except (TypeError, ValueError):
            max_attempts = WP_A3_DEFAULT_MAX_ATTEMPTS
        claim_ref = str(directive.get("claim_ref") or "")
        scope = _normalize_scope(directive.get("scope"))
        turn = int(iteration)
        order_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"requery-order|{claim_ref}|{query}|{scope}|{turn}",
        )
        return {
            "kind": "work_order",
            "order_kind": WorkOrderKind.REQUERY.value,
            "iteration": turn,
            "turn_issued": turn,
            "claim_ref": claim_ref,
            "query": query,
            "scope": scope,
            "max_attempts": max_attempts,
            "consumed": False,
            "order": {
                "work_order_id": str(order_id),
                "kind": WorkOrderKind.REQUERY.value,
                "action_type": action_type,
                "action_parameters": params,
                "addresses": {
                    "triage_route": "UNKNOWN",
                    "claim_kind": "untyped",
                    "has_unbeaten_rival": False,
                },
                "cost_estimate": 1,
                "max_attempts": max_attempts,
                "seed_source": f"verify:{turn}",
                "status": WorkOrderStatus.PENDING.value,
            },
        }
    except Exception:
        return None


def _should_force_order_requery(
    action_history: list[Any] | None,
    recorded_summaries: list[Any] | None,
) -> dict[str, Any] | None:
    """G3 order forcing decision (2b step 5: dispatch from carriers).

    Replaces ``_should_force_ledger_requery``: returns the typed
    override (``action_type``, ``action_parameters``, ``order_index``)
    for the newest unconsumed, budget-satisfying carried REQUERY order,
    or ``{"mark_consumed": "budget-exhausted", "order_index": index}``
    when the newest unconsumed carrier budgeted out, or None when the
    turn proceeds unchanged. Attempt-budget matching is unchanged
    (same stamps, same summaries) — only the carrier shape changed
    from marker dict to order entry. Reads only workflow history plus
    same-turn summaries. Pure — workflow-safe.

    Precedence is enforced by the caller: stack-walk suppresses
    evaluation entirely, and every later override stands down once
    ``forced_requery`` is set.
    """
    try:
        entries = list(action_history or [])
    except Exception:
        return None
    try:
        recorded = list(recorded_summaries or [])
    except Exception:
        recorded = []
    candidate_index: int | None = None
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") != "work_order":
            continue
        if entry.get("order_kind") != WorkOrderKind.REQUERY.value:
            continue
        if entry.get("consumed"):
            continue
        candidate_index = index
        break
    if candidate_index is None:
        return None
    carrier = entries[candidate_index]
    try:
        consumed = directive_consumed(carrier, entries, recorded)
    except Exception:
        return None
    if consumed:
        return {"mark_consumed": "budget-exhausted", "order_index": candidate_index}
    try:
        payload = carrier.get("order")
        order = WorkOrder.model_validate(payload) if isinstance(payload, dict) else None
    except Exception:
        order = None
    if order is None or order.status != WorkOrderStatus.PENDING:
        return {"mark_consumed": "budget-exhausted", "order_index": candidate_index}
    return {
        "action_type": order.action_type,
        "action_parameters": dict(order.action_parameters or {}),
        "order_index": candidate_index,
    }


# D3: failure triage routes (pure, deterministic). The route guides the
# reasoner (prompt) and is recorded in trace/telemetry; it never decides
# the outcome alone (no auto-abstain — a misroute must cost at most a
# wasted turn, never a wrong verdict).
_TRIAGE_TRANSIENT_PATTERNS = (
    "timeout",
    "timed out",
    "deadline exceeded",
    "socketexception",
    "connectexception",
    "rate limit",
    "too many requests",
    "service unavailable",
    " 503",
    " 429",
    " 502",
    " 504",
)
_TRIAGE_CONFIG_PATTERNS = (
    "filenotfound",
    "no such file",
    "missing configuration",
    "invalid configuration",
    ".yml",
    ".yaml",
    ".properties",
    "config",
)
_TRIAGE_DATA_PATTERNS = (
    "missing field",
    "missing attribute",
    "missing",
    "incorrect value",
    "incorrect",
    "wrong offers",
    "wrong offer",
    "wrong",
    "expected",
    "null value",
)


# E1: rule-based iteration-0 plans per triage route. Deterministic strategy
# skeletons the loop checks off; deviations stay visible in the trace.
# Kept to capability names the loop actually dispatches.
_ROUTE_PLANS: dict[str, list[str]] = {
    "BUG": ["WALK_STACK", "GET_CODE", "SEARCH_LOGS", "VERIFY_HYPOTHESIS"],
    "DATA": ["SEARCH_LOGS", "QUERY_STATE", "TRACE_LOGIC", "VERIFY_HYPOTHESIS"],
    "TRANSIENT": ["SEARCH_LOGS", "QUERY_STATE", "CONCLUDE"],
    "CONFIG": ["QUERY_STATE", "GET_CODE", "VERIFY_HYPOTHESIS"],
    "UNKNOWN": ["SEARCH_LOGS", "SEARCH_CODE", "FORMULATE_HYPOTHESIS"],
}


def _investigation_plan(triage_route: str, intake_seed: dict | None) -> list[str]:
    """Strategy skeleton for the run (pure, deterministic).

    E1: route selects the skeleton; intake frames prepend WALK_STACK when
    present (mirrors the forcing priority). Bounded at 5 steps.
    """
    try:
        route = str(triage_route or "UNKNOWN").upper()
        plan = list(_ROUTE_PLANS.get(route, _ROUTE_PLANS["UNKNOWN"]))
        seed = intake_seed or {}
        frames = [f for f in (seed.get("frames") or []) if isinstance(f, dict)]
        if frames and plan and plan[0] != "WALK_STACK":
            plan = ["WALK_STACK"] + [step for step in plan if step != "WALK_STACK"]
        return plan[:5]
    except Exception:
        return list(_ROUTE_PLANS["UNKNOWN"])


def _classify_failure(description: str | None, intake_seed: dict | None) -> str:
    """Triage route for a failure: TRANSIENT/CONFIG/DATA/BUG/UNKNOWN.

    Precedence: explicit golden pair → DATA; error-class transient
    signature → TRANSIENT; config signature → CONFIG; data language →
    DATA; frames/exception tokens → BUG; else UNKNOWN. Pure — workflow-safe.
    """
    try:
        seed = intake_seed or {}
        if seed.get("expected") or seed.get("actual"):
            return "DATA"
        text = f" {(description or '').lower()} "
        error_class = str(seed.get("error_class", "") or "").lower()
        haystacks = (error_class, text)
        if any(pattern in hay for hay in haystacks for pattern in _TRIAGE_TRANSIENT_PATTERNS):
            return "TRANSIENT"
        if any(pattern in hay for hay in haystacks for pattern in _TRIAGE_CONFIG_PATTERNS):
            return "CONFIG"
        if any(pattern in hay for hay in haystacks for pattern in _TRIAGE_DATA_PATTERNS):
            return "DATA"
        if seed.get("frames"):
            return "BUG"
        if "exception" in error_class or "error" in error_class:
            return "BUG"
        return "UNKNOWN"
    except Exception:
        return "UNKNOWN"


# E3: replan trigger — two consecutive EMPTY turns on the same surface
# force a different capability family (structurally, not by prompt plea).
# Only SEARCH_LOGS <-> SEARCH_CODE have safely constructible params.
_REPLAN_ALTERNATIVES = ("SEARCH_LOGS", "SEARCH_CODE")
_REPLAN_STOPWORDS = frozenset(
    {
        "what",
        "where",
        "when",
        "which",
        "does",
        "doing",
        "have",
        "has",
        "had",
        "with",
        "from",
        "that",
        "this",
        "these",
        "those",
        "there",
        "their",
        "about",
        "show",
        "recent",
        "service",
        "services",
    }
)


def _description_keywords(description: str | None, limit: int = 6) -> list[str]:
    """Noun-ish keywords from the complaint (pure, deterministic)."""
    try:
        keywords: list[str] = []
        for token in re.split(r"[^a-z0-9]+", f" {(description or '').lower()} "):
            if len(token) >= 4 and token not in _REPLAN_STOPWORDS:
                if token not in keywords:
                    keywords.append(token)
            if len(keywords) >= limit:
                break
        return keywords
    except Exception:
        return []


_FLOOR_TERMINAL_ACTIONS = frozenset({"FORMULATE_HYPOTHESIS", "VERIFY_HYPOTHESIS", "CONCLUDE"})
_FLOOR_MIN_EVIDENCE = 3


def _callsite_target(
    reason_data: dict[str, Any] | None,
    action_history: list[dict[str, Any]] | None,
    forced_count: int,
    *,
    tenant_id: str = "",
    investigation_id: str = "",
) -> dict[str, Any] | None:
    """GET_CODE for a claimed caller file never successfully read, or None.

    Fires when a CALL_EDGE claim names a target file_path that no
    evidence-banking GET_CODE has covered yet — the call-site proof the
    verifier demands (L2 died twice with caller names but no call-site
    row). Reads the target's line window when the claim supplies one.
    Bounded (max 2 per run); pure. Content-free by design: row contents
    are unavailable at workflow scope, so file identity + history
    coverage decide.
    """
    try:
        if int(forced_count or 0) >= 2:
            return None
        claims = (reason_data or {}).get("claims") or []
        if not isinstance(claims, list):
            return None

        def _edge_identity(target: dict[str, Any]) -> tuple[str, ...]:
            path = str(target.get("file_path", "") or "").replace("\\", "/")
            path = path.strip().strip("/").lstrip("./")
            try:
                start = int(target.get("line_start", target.get("start_line")) or 0) or 0
                end = int(target.get("line_end", target.get("end_line")) or 0) or 0
            except (TypeError, ValueError):
                start, end = 0, 0
            return (
                str(target.get("symbol", "") or "").strip(),
                str(target.get("counterpart", "") or "").strip(),
                path,
                str(target.get("commit_id", target.get("revision", "")) or "").strip(),
                str(start),
                str(end),
                str(target.get("scope", "") or "").strip(),
                str(target.get("tenant_id", tenant_id) or "").strip(),
                str(target.get("investigation_id", investigation_id) or "").strip(),
            )

        read_files: set[str] = set()
        read_obligations: set[tuple[str, ...]] = set()
        for entry in action_history or []:
            try:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("action", "") or "").upper() != "GET_CODE":
                    continue
                if int(entry.get("evidence_added", 0) or 0) <= 0:
                    continue
                params = entry.get("parameters") or {}
                path = str(params.get("file_path", "") or "").strip()
                if path and params.get("caller_edge_key"):
                    read_target = {
                        "symbol": params.get("caller_edge_symbol", ""),
                        "counterpart": params.get("caller_edge_counterpart", ""),
                        "file_path": path,
                        "commit_id": params.get("revision", ""),
                        "line_start": params.get("start_line"),
                        "line_end": params.get("end_line"),
                        "scope": params.get("caller_edge_scope", ""),
                        "tenant_id": params.get("caller_edge_tenant_id", ""),
                        "investigation_id": params.get("caller_edge_investigation_id", ""),
                    }
                    read_obligations.add(_edge_identity(read_target))
                if path:
                    read_files.add(path.replace("\\", "/").strip().strip("/"))
            except (TypeError, ValueError):
                continue
        for raw_claim in claims:
            try:
                if not isinstance(raw_claim, dict):
                    continue
                edge_candidates = raw_claim.get("caller_edges")
                if isinstance(edge_candidates, list) and edge_candidates:
                    candidates = [
                        edge.get("target")
                        for edge in edge_candidates
                        if isinstance(edge, dict) and isinstance(edge.get("target"), dict)
                    ]
                else:
                    candidates = [raw_claim.get("target")]
                for raw_target in candidates:
                    if not isinstance(raw_target, dict):
                        continue
                    target = raw_target
                    kind = str(raw_claim.get("claim_kind", "") or "").strip().upper()
                    if kind != "CALL_EDGE":
                        continue
                    file_path = str(target.get("file_path", "") or "").strip()
                    if not file_path:
                        continue
                    normalized = file_path.replace("\\", "/").strip().strip("/").lstrip("./")

                    # Must match VerificationEngine.canonical_caller_edge_key:
                    # identical normalization, `|` escaped as `%7C`.
                    def _escape(value: Any) -> str:
                        return str(value or "").strip().replace("|", "%7C")

                    call_params: dict[str, Any] = {
                        "file_path": file_path,
                        "caller_edge_key": "|".join(
                            (
                                _escape(target.get("symbol", "")),
                                _escape(target.get("counterpart", "")),
                                normalized.replace("|", "%7C"),
                                _escape(target.get("commit_id", target.get("revision", ""))),
                            )
                        ),
                        "caller_edge_symbol": str(target.get("symbol", "") or ""),
                        "caller_edge_counterpart": str(target.get("counterpart", "") or ""),
                        "caller_edge_scope": str(target.get("scope", "") or ""),
                        "caller_edge_tenant_id": str(tenant_id or ""),
                        "caller_edge_investigation_id": str(investigation_id or ""),
                    }
                    try:
                        start = int(target.get("line_start", target.get("start_line")) or 0) or None
                        end = int(target.get("line_end", target.get("end_line")) or 0) or None
                    except (TypeError, ValueError):
                        start, end = None, None
                    if start and start >= 1:
                        call_params["start_line"] = start
                        if end and end >= start:
                            call_params["end_line"] = end
                    revision = str(
                        target.get("commit_id", target.get("revision", "")) or ""
                    ).strip()
                    if revision:
                        call_params["revision"] = revision
                    if _edge_identity(target) in read_obligations:
                        continue
                    return {"next_action": "GET_CODE", "action_parameters": call_params}
                kind = raw_claim.get("claim_kind", "")
                kind_value = str(getattr(kind, "value", kind) or "").strip().upper()
                target = raw_claim.get("target") or {}
                if not isinstance(target, dict):
                    continue
                value_semantics = str(raw_claim.get("value_semantics") or "").upper()
                if kind_value == "DEFINITION" and value_semantics in {
                    "EXPLICIT_INITIALIZER",
                    "LANGUAGE_DEFAULT",
                }:
                    field_name = str(target.get("symbol", "") or "").strip()
                    file_path = str(target.get("file_path", "") or "").strip()
                    if not field_name or not file_path:
                        continue
                    normalized = file_path.replace("\\", "/").strip().strip("/")
                    if normalized in read_files:
                        continue
                    start_raw = target.get("line_start", target.get("start_line"))
                    try:
                        start = int(start_raw or 0) or None
                    except (TypeError, ValueError):
                        start = None
                    params: dict[str, Any] = {"file_path": file_path, "field_name": field_name}
                    if target.get("declaring_type"):
                        params["declaring_type"] = str(target["declaring_type"])
                    revision = str(target.get("revision", "") or "").strip()
                    if revision:
                        params["revision"] = revision
                    if start and start >= 1:
                        params["start_line"] = start
                        params["end_line"] = max(start, int(target.get("line_end") or start))
                    return {"next_action": "GET_CODE", "action_parameters": params}
                if kind_value != "CALL_EDGE":
                    continue
                file_path = str(target.get("file_path", "") or "").strip()
                if not file_path:
                    continue
                normalized = file_path.replace("\\", "/").strip().strip("/")
                call_params: dict[str, Any] = {"file_path": file_path}
                try:
                    # ClaimTarget serializes the window as line_start/line_end.
                    start_raw = target.get("line_start", target.get("start_line"))
                    end_raw = target.get("line_end", target.get("end_line"))
                    start = int(start_raw or 0) or None
                    end = int(end_raw or 0) or None
                except (TypeError, ValueError):
                    start, end = None, None
                if start and start >= 1:
                    call_params["start_line"] = start
                    if end and end >= start:
                        call_params["end_line"] = end
                call_params.update(
                    {
                        "caller_edge_key": "|".join(
                            (
                                str(target.get("symbol", "") or "").strip().replace("|", "%7C"),
                                str(target.get("counterpart", "") or "")
                                .strip()
                                .replace("|", "%7C"),
                                normalized.replace("|", "%7C"),
                                str(target.get("commit_id", target.get("revision", "")) or "")
                                .strip()
                                .replace("|", "%7C"),
                            )
                        ),
                        "caller_edge_symbol": str(target.get("symbol", "") or ""),
                        "caller_edge_counterpart": str(target.get("counterpart", "") or ""),
                        "caller_edge_scope": str(target.get("scope", "") or ""),
                        "caller_edge_tenant_id": str(tenant_id or ""),
                        "caller_edge_investigation_id": str(investigation_id or ""),
                    }
                )
                revision = str(target.get("commit_id", target.get("revision", "")) or "").strip()
                if revision:
                    call_params["revision"] = revision
                if _edge_identity(target) in read_obligations:
                    continue
                return {"next_action": "GET_CODE", "action_parameters": call_params}
            except Exception:
                continue
        return None
    except Exception:
        return None


def _absence_disconfirmation_target(
    reason_data: dict[str, Any] | None,
    evidence: list[Any] | None,
    action_history: list[dict[str, Any]] | None,
    forced_count: int,
) -> dict[str, Any] | None:
    """Force one provider-located source read before an ABSENCE claim verifies."""
    try:
        if int(forced_count or 0) >= 2:
            return None
        claims = (reason_data or {}).get("claims") or []
        if not isinstance(claims, list):
            return None
        absence_ids: set[str] = set()
        absence_scopes: set[str] = set()
        for claim in claims:
            if not isinstance(claim, dict) or str(claim.get("claim_kind", "")).upper() != "ABSENCE":
                continue
            target = claim.get("target") or {}
            if isinstance(target, dict) and str(target.get("scope", "") or "").strip():
                absence_scopes.add(str(target["scope"]).strip().casefold())
            for raw_id in claim.get("evidence_ids") or []:
                if isinstance(raw_id, str):
                    absence_ids.add(raw_id)
        if not absence_scopes:
            return None
        read_files: set[str] = set()
        for action in action_history or []:
            if not isinstance(action, dict) or str(action.get("action", "")).upper() != "GET_CODE":
                continue
            params = action.get("parameters") or {}
            path = str(params.get("file_path", "") or "").strip()
            if path:
                read_files.add(path.replace("\\", "/").strip().strip("/").lstrip("./"))
        candidates: list[tuple[str, Any, dict[str, Any]]] = []

        def _get(item: Any, key: str, default: Any = None) -> Any:
            if isinstance(item, dict):
                return item.get(key, default)
            return getattr(item, key, default)

        for item in evidence or []:
            evidence_id = str(_get(item, "evidence_id", "") or "")
            if absence_ids and evidence_id not in absence_ids:
                continue
            provenance = _get(item, "provenance", {}) or {}
            location = _get(provenance, "source_location", provenance) or {}
            path = str(_get(location, "file_path", "") or "").strip()
            start = _get(location, "line_start")
            end = _get(location, "line_end")
            if not path or not isinstance(start, int) or not isinstance(end, int) or end < start:
                continue
            operation = str(
                _get(_get(provenance, "query_fingerprint", provenance), "operation", "") or ""
            ).upper()
            if operation not in {"SEARCH_CODE", "FIND_SYMBOL", "FIND_CALL_GRAPH"}:
                continue
            attrs = _get(item, "attributes", {}) or {}
            if not isinstance(attrs, dict):
                continue
            match_count = attrs.get("match_count")
            if (
                not isinstance(match_count, int)
                or isinstance(match_count, bool)
                or match_count <= 0
            ):
                continue
            if operation != "SEARCH_CODE" and attrs.get("discovery_only") is not True:
                continue
            scope = str(attrs.get("search_scope", "") or "").strip().casefold()
            if scope not in absence_scopes:
                continue
            normalized_path = path.replace("\\", "/").strip().strip("/").lstrip("./")
            if normalized_path in read_files:
                continue
            revision = str(_get(location, "revision", "") or "").strip()
            tenant = str(_get(item, "tenant_id", "") or "").strip()
            investigation = str(_get(item, "investigation_id", "") or "").strip()
            if not tenant:
                continue
            obligation_material = "|".join(
                (
                    tenant,
                    investigation,
                    scope,
                    evidence_id,
                    normalized_path,
                    revision,
                    str(start),
                    str(end),
                )
            )
            key = (
                "ABSENCE_DISCONFIRMATION|"
                + hashlib.sha256(obligation_material.encode()).hexdigest()
            )
            params: dict[str, Any] = {
                "file_path": path,
                "start_line": start,
                "end_line": end,
                "absence_disconfirmation_key": key,
            }
            if revision:
                params["revision"] = revision
            candidates.append((normalized_path, item, {"key": key, "parameters": params}))
        if not candidates:
            return None
        candidates.sort(key=lambda entry: (entry[0], str(_get(entry[1], "evidence_id", ""))))
        return {
            "next_action": "GET_CODE",
            "action_parameters": candidates[0][2]["parameters"],
        }
    except Exception:
        return None


def _explore_floor_target(
    reason_data: dict[str, Any] | None,
    evidence_banked_total: int,
    triage_route: str | None,
    description: str | None,
    seed: dict[str, Any] | None,
    forced_count: int,
    history: list[Any] | None = None,
) -> dict[str, Any] | None:
    """Discriminating gather action before thin terminal proposals, or None.

    Fires when the model proposes a committal action (formulate/verify/
    conclude) with fewer than 3 banked rows on a code-shaped incident.
    L1#2 died after a single path-only read: the absence branch it took
    can never verify, so the loop must read first instead of letting the
    run commit to a one-row theory. Bounded (max 2 per run); pure.

    ``history`` dedupes targets: a file already read, a symbol already
    resolved, or a query already searched is never re-dispatched, so the
    floor cannot burn its cap re-fetching what is already banked.
    """
    try:
        data_action = str((reason_data or {}).get("next_action", "") or "").upper()
        if data_action not in _FLOOR_TERMINAL_ACTIONS:
            return None
        if int(forced_count or 0) >= 2:
            return None
        # P2 (S4): the reason activity reports shape-aware readiness from
        # banked rows. Ready means qualified evidence is banked — no floor
        # gather. Not-ready means gather until qualified (the legacy count
        # gate is skipped: 3 unqualified rows must not satisfy the floor).
        # Absent (legacy/undecidable) preserves the count-based floor.
        s4 = (reason_data or {}).get("s4_ready", None)
        if s4 is True:
            return None
        s4_floor = s4 is False
        if not s4_floor:
            try:
                if int(evidence_banked_total or 0) >= _FLOOR_MIN_EVIDENCE:
                    return None
            except (TypeError, ValueError):
                return None
        seed = seed or {}
        frames = seed.get("frames") or []
        code_shaped = (
            str(triage_route or "").upper() == "BUG"
            or bool(frames)
            or bool(str(seed.get("failing_symbol", "") or "").strip())
            or bool(str(seed.get("caller_symbol", "") or "").strip())
        )
        if not code_shaped:
            try:
                text = f" {(description or '').lower()} "
                code_shaped = bool(
                    re.search(r"[a-z0-9_.\-/]*\.[a-z]{1,10}", text) and re.search(r"[a-z]+", text)
                ) and (
                    "file" in text
                    or "implement" in text
                    or "defin" in text
                    or "symbol" in text
                    or "code" in text
                    or "call" in text
                )
            except Exception:
                code_shaped = False
        if not code_shaped and not s4_floor:
            return None
        s4_shape = str((reason_data or {}).get("s4_shape") or "").strip().lower()
        if s4_floor and s4_shape == "log":
            # P2 review: a not-ready log question must gather from the log
            # layer, never code reads. Keywords mirror the SEARCH_CODE
            # fallback below; SEARCH_LOGS takes a keyword list.
            log_keywords = _description_keywords(description)
            if not log_keywords:
                return None
            return {
                "next_action": "SEARCH_LOGS",
                "action_parameters": {"keywords": log_keywords[:5]},
            }
        if s4_floor and s4_shape == "state":
            # P2 review: a not-ready state question must gather from the
            # state layer. No template binds are derivable here, so the
            # default template applies (fail-open gather, may miss); the
            # bound (max 2/run) caps the cost of a blind probe.
            return {"next_action": "QUERY_STATE", "action_parameters": {}}
        seen_files, seen_symbols, seen_queries = _floor_seen_targets(history)
        haystacks = [str(description or "")]
        try:
            for key in ("top_frame_file", "failing_symbol", "caller_symbol"):
                value = str(seed.get(key, "") or "").strip()
                if value:
                    haystacks.append(value)
        except Exception:
            pass
        for text in haystacks:
            try:
                for match in re.finditer(_REREAD_PATH_TOKEN_RE, text):
                    token = match.group(0).strip().strip(".,;:)]}'\"").lstrip("./")
                    if "/" in token and len(token) >= 3 and token not in seen_files:
                        return {
                            "next_action": "GET_CODE",
                            "action_parameters": {"file_path": token},
                        }
            except Exception:
                continue
        for key in ("failing_symbol", "caller_symbol"):
            try:
                symbol = str(seed.get(key, "") or "").strip()
                if symbol and symbol not in seen_symbols:
                    return {
                        "next_action": "FIND_SYMBOL",
                        "action_parameters": {"symbol_name": symbol},
                    }
            except Exception:
                continue
        keywords = _description_keywords(description)
        if not keywords:
            return None
        query = " ".join(keywords[:3])
        if query in seen_queries:
            return None
        return {
            "next_action": "SEARCH_CODE",
            "action_parameters": {"query": query},
        }
    except Exception:
        return None


def _floor_seen_targets(history: list[Any] | None) -> tuple[set[str], set[str], set[str]]:
    """Files read, symbols resolved, and queries searched so far (pure).

    Paths are normalised (backslashes, leading ``./``) so ``./a/B.java`` and
    ``a/B.java`` dedupe. Reads only the recorded ``parameters`` of dispatched
    turns; carrier entries have none and are ignored. Never raises.
    """
    files: set[str] = set()
    symbols: set[str] = set()
    queries: set[str] = set()
    try:
        for entry in history or []:
            if not isinstance(entry, dict):
                continue
            params = entry.get("parameters")
            if not isinstance(params, dict):
                continue
            path = str(params.get("file_path") or "").strip().replace("\\", "/")
            while path.startswith("./"):
                path = path[2:]
            if path:
                files.add(path)
            symbol = str(params.get("symbol_name") or "").strip()
            if symbol:
                symbols.add(symbol)
            query = str(params.get("query") or "").strip()
            if query:
                queries.add(query)
    except Exception:
        pass
    return files, symbols, queries


def _should_force_replan(
    description: str | None,
    reason_data: dict | None,
    action_history: list | None,
    forced_count: int,
    *,
    separate_carriers: bool = False,
) -> dict | None:
    """Alternative gathering action after repeated empties, or None.

    Fires when the two most recent enriched turns share one action with no
    evidence banked on either, and the current proposal repeats it. The
    production call site runs before the current action is appended, so
    the proposal arrives via reason_data (not history): history holds only
    completed enriched turns. Returns {next_action, params} for the first
    alternative family, or None. Bounded (max 2 per run). Pure.
    """
    try:
        if int(forced_count or 0) >= 2:
            return None
        # F2 (lv6): carriers are not turns; a trailing carrier used to displace
        # the second prior turn and silently disable this rule.
        entries = (
            turn_entries(action_history)
            if separate_carriers
            else [e for e in (action_history or []) if isinstance(e, dict)]
        )
        # Production shape: completed enriched turns only. The last two
        # are the prior turns; the current proposal comes from reason_data.
        if len(entries) < 2:
            return None
        prior_two = entries[-2:]
        actions = [str(e.get("action", "") or "").upper() for e in prior_two]
        if not actions[0] or actions[0] != actions[1]:
            return None
        if actions[0] not in _REPLAN_ALTERNATIVES:
            return None
        for entry in prior_two:
            try:
                if int(entry.get("evidence_added", 0) or 0) > 0:
                    return None
            except (TypeError, ValueError):
                continue
        data_action = ""
        try:
            data_action = str((reason_data or {}).get("next_action", "") or "").upper()
        except Exception:
            pass
        if data_action != actions[0]:
            return None
        alternative = next((a for a in _REPLAN_ALTERNATIVES if a != actions[0]), None)
        if alternative is None:
            return None
        keywords = _description_keywords(description)
        if not keywords:
            return None
        if alternative == "SEARCH_LOGS":
            params: dict[str, Any] = {"keywords": keywords, "services": []}
        else:
            params = {"query": " ".join(keywords[:3])}
        return {"next_action": alternative, "action_parameters": params}
    except Exception:
        return None


# T2-4: synthesis forcing for rich-but-unformulated corpora (the L5
# shape): many banked rows across distinct gathering actions, zero
# committed formulations. Distinct actions proxy provider diversity
# (available in history; provider names are not).
_SYNTHESIS_MIN_ROWS = 8


def _should_force_synthesis(
    action_history: list | None,
    evidence_banked_total: int,
    formulated_count: int,
    forced_count: int,
    reason_data: dict | None = None,
) -> bool:
    """Whether to force FORMULATE_HYPOTHESIS on a rich corpus (pure).

    Fires when ≥8 rows banked across ≥2 distinct gathering actions with
    nothing formulated, the model proposes another gathering action, and
    the cap (2/run) holds. Skipped when the current proposal is already
    FORMULATE/VERIFY/CONCLUDE. Pure — workflow-safe.
    """
    try:
        if int(formulated_count or 0) > 0 or int(forced_count or 0) >= 2:
            return False
        if int(evidence_banked_total or 0) < _SYNTHESIS_MIN_ROWS:
            return False
        try:
            proposed = str((reason_data or {}).get("next_action", "") or "").upper()
        except Exception:
            proposed = ""
        if proposed in ("FORMULATE_HYPOTHESIS", "VERIFY_HYPOTHESIS", "CONCLUDE"):
            return False
        actions: set[str] = set()
        for entry in action_history or []:
            try:
                if not isinstance(entry, dict):
                    continue
                if int(entry.get("evidence_added", 0) or 0) <= 0:
                    continue
                action = str(entry.get("action", "") or "").upper()
                if action and action != "FORMULATE_HYPOTHESIS":
                    actions.add(action)
            except (TypeError, ValueError):
                continue
        return len(actions) >= 2
    except Exception:
        return False


def _should_force_log_search(
    description: str | None,
    reason_data: dict | None,
    action_history: list | None,
    forced_count: int,
) -> dict | None:
    """SEARCH_LOGS parameters to force, or None.

    Fires when the complaint uses error/log language as whole words, the
    model proposes a non-log gathering action, no SEARCH_LOGS has banked
    evidence yet, and no prior SEARCH_LOGS was denied for this tenant.
    Keywords come from the proposed SEARCH_CODE query when present, else
    from complaint nouns. Bounded (max 2 per run). Pure — workflow-safe.
    """
    try:
        data = reason_data or {}
        if str(data.get("next_action", "")).upper() not in _LOG_FORCE_FROM_ACTIONS:
            return None
        if int(forced_count or 0) >= 2:
            return None
        lowered_desc = f" {(description or '').lower()} "
        if not any(rx.search(lowered_desc) for rx in _LOG_FORCE_ERROR_RES):
            return None
        for entry in action_history or []:
            try:
                action = str((entry or {}).get("action", "")).upper()
                evidenced = int((entry or {}).get("evidence_added", 0) or 0)
                miss_reason = str((entry or {}).get("miss_reason", "") or "")
                outcome_status = str((entry or {}).get("outcome_status", "") or "").upper()
            except (TypeError, ValueError):
                continue
            if action == "SEARCH_LOGS" and evidenced > 0:
                return None
            if (
                action == "SEARCH_LOGS"
                and evidenced <= 0
                and (
                    miss_reason in _LOG_FORCE_DENIED_MISS_REASONS
                    or outcome_status in _LOG_FORCE_DENIED_STATUSES
                )
            ):
                return None
        params = data.get("action_parameters") or {}
        query = str(params.get("query", "") or "").strip()
        keywords = [query] if query else []
        if not keywords:
            for token in re.split(r"[^a-z0-9]+", lowered_desc):
                if len(token) >= 4 and token not in _LOG_FORCE_STOPWORDS:
                    if token not in keywords:
                        keywords.append(token)
                if len(keywords) >= 6:
                    break
        if not keywords:
            return None
        return {"keywords": keywords, "services": []}
    except Exception:
        return None


def _reread_target(
    reason_data: dict | None,
    action_history: list | None,
    forced_count: int,
) -> str | None:
    """File path to force a GET_CODE range-read on, or None.

    Fires when the model proposes VERIFY_HYPOTHESIS over a
    definition-strength claim ("implemented in X") with no successful
    range-read in history yet — the G3 floor would INCONCLUSIVE the
    verify, so the loop reads first instead of wasting the turn.
    Bounded (max 2 forced re-reads per run); returns None when no path
    is extractable (nothing constructible to read). Pure and
    deterministic — workflow-safe.
    """
    try:
        data = reason_data or {}
        if str(data.get("next_action", "")).upper() != "VERIFY_HYPOTHESIS":
            return None
        if int(forced_count or 0) >= 2:
            return None
        raw_hyps = data.get("hypotheses_evaluated") or []
        statements = []
        for item in raw_hyps:
            if isinstance(item, str):
                statements.append(item)
            elif isinstance(item, dict):
                text = item.get("statement") or item.get("text") or ""
                statements.append(str(text))
        lowered = [f" {s.lower()} " for s in statements]
        if not any(pattern in text for text in lowered for pattern in _REREAD_DEFINITION_PATTERNS):
            return None
        for entry in action_history or []:
            try:
                action = str((entry or {}).get("action", "")).upper()
                evidenced = int((entry or {}).get("evidence_added", 0) or 0)
            except (TypeError, ValueError):
                continue
            if action in _REREAD_GET_OPERATIONS and evidenced > 0:
                return None
        for statement in statements:
            for match in re.finditer(_REREAD_PATH_TOKEN_RE, statement):
                token = match.group(0).strip().strip(".,;:)]}'\"").lstrip("./")
                if "/" in token and len(token) >= 3:
                    return token
        return None
    except Exception:
        return None


def _caused_by_idempotency_wedge(exc: BaseException) -> bool:
    """Whether an ActivityError wraps an idempotency-wedge failure (pure).

    Tranche B: walks __cause__/__context__ (bounded) for
    type == "IdempotencyRequestInProgress". Anything else → False → raise.
    """
    try:
        seen = 0
        cause: BaseException | None = exc
        while cause is not None and seen < 5:
            if getattr(cause, "type", "") == "IdempotencyRequestInProgress":
                return True
            cause = getattr(cause, "__cause__", None) or getattr(cause, "__context__", None)
            if not isinstance(cause, BaseException):
                return False
            seen += 1
        return False
    except Exception:
        return False


def _caused_by_reasoning_transient(exc: BaseException) -> bool:
    """Whether an ActivityError wraps an exhausted transient reasoning failure (pure).

    H7: reason_activity raises a retryable ``ReasoningProviderTransient`` for a
    timeout / 5xx / throttle. When Temporal's retries exhaust, the SDK surfaces
    it as ActivityError; the workflow degrades the run instead of failing it.
    Walks __cause__/__context__ (bounded) like the idempotency-wedge check;
    anything else -> False -> the caller re-raises.
    """
    try:
        seen = 0
        cause: BaseException | None = exc
        while cause is not None and seen < 5:
            if getattr(cause, "type", "") == TRANSIENT_FAILURE_TYPE:
                return True
            cause = getattr(cause, "__cause__", None) or getattr(cause, "__context__", None)
            if not isinstance(cause, BaseException):
                return False
            seen += 1
        return False
    except Exception:
        return False


def _is_auth_blocked(exec_data: Any) -> bool:
    """True when the executed action blocked on provider auth (pure).

    Phase 3.1: an expired credential cannot be worked around by trying
    other capabilities, so the loop must halt rather than publish a thin
    answer. Narrowly keyed on auth_failed — other BLOCKED outcomes
    (e.g. authorization denial) keep their existing loop behavior.
    """
    try:
        outcome = (exec_data or {}).get("action_outcome") or {}
        return outcome.get("status") == "BLOCKED" and outcome.get("miss_reason") == "auth_failed"
    except Exception:
        return False


def _degraded_empty_result(action: str, miss_reason: str) -> Any:
    """Synthetic degraded-empty execute result (pure, workflow-safe).

    Tranche B: shapes the same data contract as a real degraded execute
    (action_outcome + items_count) so downstream history enrichment,
    input checks, and budget guards behave identically.
    """
    from types import SimpleNamespace as _SimpleNamespace

    return _SimpleNamespace(
        data={
            "action": action,
            "items_count": 0,
            "action_outcome": {
                "status": "DEGRADED",
                "miss_reason": miss_reason,
                "evidence_ids": [],
                "item_count": 0,
            },
        }
    )


# Phase 4 (claim binding): smallest-fetch forcing. Pure, deterministic,
# replay-safe helpers (no I/O, no clock) following the _callsite_target
# precedent. Gap identity is the group identity (kind|target_key);
# claim_ref rides along as informational context only.

SMALLEST_FETCH_ORDER: tuple[str, ...] = (
    "DEFINITION",
    "HISTORY",
    "BEHAVIOR",
    "ABSENCE",
    "CAUSAL",
)

SMALLEST_FETCH_ACTIONS_BY_KIND: dict[str, frozenset[str]] = {
    "DEFINITION": frozenset({"GET_CODE"}),
    "HISTORY": frozenset({"GET_CODE_HISTORY"}),
    "BEHAVIOR": frozenset({"SEARCH_LOGS", "QUERY_STATE"}),
    "ABSENCE": frozenset({"SEARCH_CODE"}),
    "CAUSAL": frozenset({"GET_CODE"}),
}

_FORCED_SMALLEST_FETCH_DEFAULT_CAP = 2


def _definition_fetch_params(gap: dict[str, Any]) -> dict[str, Any] | None:
    """GET_CODE params for a DEFINITION gap (pure)."""
    try:
        _raw_target = gap.get("target")
        target: dict[str, Any] = _raw_target if isinstance(_raw_target, dict) else {}
        file_path = str(target.get("file_path", "") or "").strip()
        if not file_path:
            return None
        params: dict[str, Any] = {"file_path": file_path}
        try:
            start = int(target.get("line_start") or 0)
            end = int(target.get("line_end") or 0)
        except (TypeError, ValueError):
            start, end = 0, 0
        if start >= 1:
            params["start_line"] = start
        if end >= 1:
            params["end_line"] = end
        scope = str(gap.get("scope") or "").strip()
        # F2: gap scope carries Phase 2 search-scope semantics (a path
        # prefix), not a revision. Only forward machine-shaped revisions
        # (HEAD or a hex SHA); anything else (path prefixes, branch names
        # with slashes) would burn a capped fire on a malformed revision.
        if scope == "HEAD" or (
            7 <= len(scope) <= 40 and all(c in "0123456789abcdefABCDEF" for c in scope)
        ):
            params["revision"] = scope
        else:
            params["revision"] = "HEAD"
        return params
    except Exception:
        return None


def _history_fetch_params(gap: dict[str, Any]) -> dict[str, Any] | None:
    """GET_CODE_HISTORY params for a HISTORY gap (pure)."""
    try:
        _raw_target = gap.get("target")
        target: dict[str, Any] = _raw_target if isinstance(_raw_target, dict) else {}
        file_path = str(target.get("file_path", "") or "").strip()
        if not file_path:
            return None
        params: dict[str, Any] = {"file_path": file_path, "limit": 10}
        symbol = str(target.get("symbol", "") or "").strip()
        if symbol:
            params["symbol"] = symbol
        return params
    except Exception:
        return None


def _behavior_fetch_params(gap: dict[str, Any]) -> dict[str, Any] | None:
    """SEARCH_LOGS (or QUERY_STATE fallback) params for a BEHAVIOR gap (pure).

    Leg selection is explicit: when the gap target names state rather
    than logs, the QUERY_STATE leg fires; otherwise SEARCH_LOGS. The
    returned directive's next_action follows the leg (both actions are in
    the BEHAVIOR allowlist); the cascade validates the pair.
    """
    try:
        _raw_target = gap.get("target")
        target: dict[str, Any] = _raw_target if isinstance(_raw_target, dict) else {}
        scope = str(target.get("scope", "") or gap.get("scope", "") or "").strip()
        symbol = str(target.get("symbol", "") or "").strip()
        query = str(gap.get("query", "") or "").strip()
        keywords = [w for w in (query or symbol).split() if w][:10]
        names_state = any(
            token in scope.lower() for token in ("state", "environment", "environ", "config")
        )
        if keywords and not names_state:
            return {"keywords": keywords, "limit": 10}
        if names_state or scope:
            return {"template_id": "default", "limit": 10, "_leg": "QUERY_STATE"}
        if keywords:
            return {"keywords": keywords, "limit": 10}
        return None
    except Exception:
        return None


def _absence_fetch_params(gap: dict[str, Any]) -> dict[str, Any] | None:
    """SEARCH_CODE params for an ABSENCE gap (pure). Empty result is proof."""
    try:
        _raw_target = gap.get("target")
        target: dict[str, Any] = _raw_target if isinstance(_raw_target, dict) else {}
        query = (
            str(gap.get("query", "") or "").strip() or str(target.get("symbol", "") or "").strip()
        )
        if not query:
            return None
        scope = str(target.get("scope", "") or gap.get("scope", "") or "").strip()
        params: dict[str, Any] = {"query": query, "revision": "HEAD", "limit": 20}
        if scope:
            params["path_prefix"] = scope
        return params
    except Exception:
        return None


def _causal_fetch_params(gap: dict[str, Any]) -> dict[str, Any] | None:
    """GET_CODE params for a CAUSAL gap's link window (pure)."""
    try:
        _raw_target = gap.get("target")
        target: dict[str, Any] = _raw_target if isinstance(_raw_target, dict) else {}
        file_path = str(target.get("file_path", "") or "").strip()
        if not file_path:
            return None
        params: dict[str, Any] = {"file_path": file_path}
        try:
            start = int(target.get("line_start") or 0)
            end = int(target.get("line_end") or 0)
        except (TypeError, ValueError):
            start, end = 0, 0
        if start >= 1:
            params["start_line"] = start
        if end >= 1:
            params["end_line"] = end
        params["revision"] = "HEAD"
        return params
    except Exception:
        return None


# Direct builder references (never string names — stringly dispatch
# fails silent under the caps).
KIND_FETCH: dict[str, dict[str, Any]] = {
    "DEFINITION": {"action": "GET_CODE", "builder": _definition_fetch_params},
    "HISTORY": {"action": "GET_CODE_HISTORY", "builder": _history_fetch_params},
    "BEHAVIOR": {"action": "SEARCH_LOGS", "builder": _behavior_fetch_params},
    "ABSENCE": {"action": "SEARCH_CODE", "builder": _absence_fetch_params},
    "CAUSAL": {"action": "GET_CODE", "builder": _causal_fetch_params},
}


def _smallest_fetch_action_for(kind: str, params: dict[str, Any]) -> str:
    """Directive action for a kind, honoring the BEHAVIOR leg (pure)."""
    try:
        spec = KIND_FETCH.get(str(kind or ""))
        if not spec:
            return ""
        if (
            str(kind or "") == "BEHAVIOR"
            and str((params or {}).pop("_leg", "") or "") == "QUERY_STATE"
        ):
            return "QUERY_STATE"
        return str(spec.get("action") or "")
    except Exception:
        return ""


def _pick_gap_order(
    orders: list[dict[str, Any]] | None,
    *,
    kind: str,
    history: list[Any] | None,
    forced_count: int,
) -> WorkOrder | None:
    """First dispatchable GAP_FILL order of one claim kind, or None (pure).

    Step-6 per-kind picker over a carrier's orders: kind match, gap not
    already covered (stamps), per-kind run cap, closed kind→action
    triple. Content-free by design: gap identity + history coverage
    decide. Never raises.
    """
    try:
        if int(forced_count or 0) >= _FORCED_SMALLEST_FETCH_DEFAULT_CAP:
            return None
    except Exception:
        return None
    try:
        want = str(kind or "")
        if want not in SMALLEST_FETCH_ORDER:
            return None
        for raw in list(orders or []):
            try:
                if not isinstance(raw, dict):
                    continue
                try:
                    order = WorkOrder.model_validate(raw)
                except Exception:
                    continue
                if order.status != WorkOrderStatus.PENDING:
                    continue
                if str(order.addresses.claim_kind or "") != want:
                    continue
                gap_id = str(order.gap_id or "")
                if not gap_id:
                    continue
                # Covered gaps fall through to the next open order of
                # the same kind — a stale carrier never suppresses its
                # siblings.
                if _smallest_fetch_already_covered(history, gap_id):
                    continue
                if not _smallest_fetch_dispatchable(
                    want, order.action_type, dict(order.action_parameters or {})
                ):
                    continue
                return order
            except Exception:
                continue
        return None
    except Exception:
        return None


def _smallest_fetch_dispatchable(kind: str, action: str, params: dict[str, Any]) -> bool:
    """Whether a (kind, action, params) triple may dispatch (pure).

    Separate validation from ledger-requery eligibility: the closed
    kind→action allowlist plus non-empty params. No ledger keys are
    fabricated here. Never raises.
    """
    try:
        allowed = SMALLEST_FETCH_ACTIONS_BY_KIND.get(str(kind or ""), frozenset())
        return str(action or "") in allowed and isinstance(params, dict) and bool(params)
    except Exception:
        return False


def _gap_order_for(gap: dict[str, Any], turn_issued: int) -> WorkOrder | None:
    """One GAP_FILL work order for a validated claim gap, or None (pure).

    Resolves the gap's fetch (KIND_FETCH builder + kind action) at carry
    time; coverage and per-kind caps stay convert-time concerns. Gap
    identity rides the order (gap_id/claim_ref), never the params.
    Deterministic ID per (gap_id, kind, turn). Never raises.
    """
    try:
        if not isinstance(gap, dict):
            return None
        kind = str(gap.get("kind") or "")
        spec = KIND_FETCH.get(kind)
        if not spec:
            return None
        gap_id = str(gap.get("gap_id") or "")
        if not gap_id:
            return None
        builder = spec.get("builder")
        try:
            params = builder(gap) if callable(builder) else None
        except Exception:
            return None
        if not params:
            return None
        try:
            action = _smallest_fetch_action_for(kind, dict(params))
            params = {k: v for k, v in dict(params).items() if not str(k).startswith("_")}
        except Exception:
            return None
        if not action:
            return None
        try:
            claim_kind = str(kind).strip().upper() or "UNTYPED"
        except Exception:
            claim_kind = "UNTYPED"
        return WorkOrder(
            work_order_id=uuid.uuid5(
                uuid.NAMESPACE_URL, f"gap-order|{gap_id}|{kind}|{int(turn_issued)}"
            ),
            kind=WorkOrderKind.GAP_FILL,
            action_type=action,
            action_parameters=params,
            addresses=FailureCharacter(triage_route="UNKNOWN", claim_kind=claim_kind[:64]),
            cost_estimate=1,
            gap_id=gap_id,
            claim_ref=str(gap.get("claim_ref") or "") or None,
            seed_source=f"gap:{gap_id}",
            status=WorkOrderStatus.PENDING,
        )
    except Exception:
        return None


def _carry_gap_orders(verify_data: dict[str, Any] | None, iteration: int) -> dict[str, Any] | None:
    """Gap order carrier to append to action_history, or None (2b step 6).

    Replaces the ``claim_gap_feed`` marker: validates verify result
    data["claim_gaps"] with the same rules (non-empty gap_id, known
    KIND_FETCH kind, dedupe by gap_id with drops counted) and returns
    ONE carrier entry whose ``orders`` payload is a GAP_FILL WorkOrder
    per buildable gap. The smallest-fetch slot dispatches from the
    newest carrier's orders; coverage stamps prevent refires, so no
    in-place consumption is needed. An empty list returns a valid
    orders-empty carrier, which supersedes older carriers (the slot
    reads the newest carrier only). Only malformed or missing input
    returns None. Pure — workflow-safe. Never raises.
    """
    try:
        data = verify_data if isinstance(verify_data, dict) else {}
        raw = data.get("claim_gaps")
        if not isinstance(raw, list):
            return None
        orders: list[dict[str, Any]] = []
        seen: set[str] = set()
        dropped_unknown_kind = 0
        dropped_missing_gap_id = 0
        dropped_duplicate = 0
        dropped_unbuildable = 0
        for entry in raw:
            try:
                if not isinstance(entry, dict):
                    dropped_missing_gap_id += 1
                    continue
                gid = str(entry.get("gap_id") or "")
                if not gid:
                    dropped_missing_gap_id += 1
                    continue
                if str(entry.get("kind") or "") not in KIND_FETCH:
                    dropped_unknown_kind += 1
                    continue
                if gid in seen:
                    dropped_duplicate += 1
                    continue
                seen.add(gid)
                order = _gap_order_for(entry, int(iteration))
                if order is None:
                    dropped_unbuildable += 1
                    continue
                orders.append(order.model_dump(mode="json"))
            except Exception:
                dropped_missing_gap_id += 1
                continue
        if raw and not orders:
            # Non-empty input but nothing dispatchable: malformed, not
            # a tombstone (mirrors the old feed carry exactly).
            return None
        return {
            "kind": "work_order_feed",
            "order_kind": WorkOrderKind.GAP_FILL.value,
            "iteration": int(iteration),
            "turn_issued": int(iteration),
            "orders": orders,
            "dropped": {
                "unknown_kind": dropped_unknown_kind,
                "missing_gap_id": dropped_missing_gap_id,
                "duplicate_gap_id": dropped_duplicate,
                "unbuildable": dropped_unbuildable,
            },
        }
    except Exception:
        return None


def _smallest_fetch_already_covered(
    action_history: list[dict[str, Any]] | None,
    gap_id: str,
) -> bool:
    """True when gap_id already fired a smallest-fetch turn (pure).

    Scans action_history newest-first for a forced_smallest_fetch_gap_id
    stamp equal to gap_id. Same gap never refires; two same-kind targets
    (different gap_ids) never suppress each other. Never raises.
    """
    try:
        want = str(gap_id or "")
        if not want:
            return False
        for evt in reversed(action_history or []):
            try:
                if not isinstance(evt, dict):
                    continue
                if str(evt.get("forced_smallest_fetch_gap_id") or "") == want:
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False


def _norm_statement(text: Any) -> str:
    """Normalized statement for claim↔evaluation joins (pure)."""
    try:
        return str(text or "").strip().casefold()
    except Exception:
        return ""


def _forced_conclude_break(reason_data: dict[str, Any] | None) -> bool:
    """Whether the loop must skip execute/retrieve/verify this turn (pure).

    True only when the D6 opinion-only fast path fired. The workflow
    breaks to CONCLUDING, which persists the honest abstention.
    Never raises.
    """
    try:
        return bool((reason_data or {}).get("forced_not_checkable", False))
    except Exception:
        return False


#: Actions that gather evidence (every ActionType except the terminal trio).
#: A literal so the sandboxed workflow needs no extra import; a unit test
#: pins it to the ActionType enum.
_GATHERING_ACTIONS = frozenset(
    {
        "SEARCH_LOGS",
        "QUERY_STATE",
        "GET_CODE",
        "SEARCH_CODE",
        "FIND_SYMBOL",
        "FIND_CALL_GRAPH",
        "GET_CODE_HISTORY",
        "PROBE_API",
        "CORRELATE",
        "WALK_STACK",
        "TRACE_LOGIC",
        "TRACE_JOURNEY",
        "WALK_SESSION_GRAPH",
        "SEARCH_REF_DOCS",
    }
)


def _is_gathering_action(action: Any) -> bool:
    """Whether a dispatched action is an evidence fetch (pure, never raises)."""
    try:
        return str(action or "").strip().upper() in _GATHERING_ACTIONS
    except Exception:
        return False


def _non_negative_count(value: Any) -> int | None:
    """A real non-negative int, else None (bool and NaN are not counts)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _d6_cap_enabled(
    evidence_banked_total: Any,
    fetches_dispatched: Any,
    loop_version: Any,
) -> bool:
    """Whether the lv4 evidence cap applies to this turn (pure, fail-open).

    True only when the run is pinned lv4+ AND both counters are valid AND
    nothing has been banked or fetched. Any fault or unrecognized pin returns
    False, which leaves D6 behaving as it did before the cap.
    """
    try:
        if not loop_version_at_least({"loop_version": loop_version}, LOOP_VERSION_LV4):
            return False
        banked = _non_negative_count(evidence_banked_total)
        fetched = _non_negative_count(fetches_dispatched)
        if banked is None or fetched is None:
            return False
        return banked == 0 and fetched == 0
    except Exception:
        return False


def _d6_decision(
    reason_data: Any,
    *,
    is_defer: bool,
    evidence_banked_total: Any,
    fetches_dispatched: Any,
    loop_version: Any,
) -> tuple[bool, bool]:
    """The D6 decision as ``(fire, blocked_by_cap)`` (pure, never raises).

    ``fire`` forces CONCLUDE for an all-NOT_CHECKABLE turn. ``blocked_by_cap``
    is True when the turn matched but the lv4 cap withheld the abstention
    because no evidence was attempted yet; the workflow counts it so the
    gate's firing rate is measurable.
    """
    try:
        if is_defer:
            return False, False
        cap = _d6_cap_enabled(evidence_banked_total, fetches_dispatched, loop_version)
        fire = _should_force_conclude_not_checkable(
            reason_data,
            evidence_banked_total=evidence_banked_total,
            fetches_dispatched=fetches_dispatched,
            cap_enabled=cap,
        )
        if fire:
            return True, False
        # Blocked only if the same turn would have fired without the cap.
        blocked = cap and _should_force_conclude_not_checkable(reason_data)
        return False, bool(blocked)
    except Exception:
        return False, False


def _should_force_conclude_not_checkable(
    reason_data: dict[str, Any] | None,
    *,
    evidence_banked_total: Any = None,
    fetches_dispatched: Any = None,
    cap_enabled: bool = False,
) -> bool:
    """True when honest abstention is the only possible outcome (pure).

    Every evaluated statement has ≥1 matching claim dict and every
    claim dict is NOT_CHECKABLE. Empty sets, unmatched statements, and
    any checkable claim return False. Never raises.

    Two decisions, kept separate: first the not-checkable match (unchanged),
    then the lv4 cap. With ``cap_enabled`` a matching turn does not force
    while the run has banked no rows and dispatched no fetch (D6 may not
    abstain before evidence was attempted). The counters are re-validated
    here; invalid counters leave the cap inert.
    """
    nc_match = _all_claims_not_checkable(reason_data)
    if not nc_match:
        return False
    if cap_enabled:
        banked = _non_negative_count(evidence_banked_total)
        fetched = _non_negative_count(fetches_dispatched)
        if banked == 0 and fetched == 0:
            return False
    return True


def _all_claims_not_checkable(
    reason_data: dict[str, Any] | None,
) -> bool:
    """The not-checkable match itself (pure). Never raises."""
    try:
        data = reason_data if isinstance(reason_data, dict) else {}
        raw_hyps = data.get("hypotheses_evaluated") or []
        if not isinstance(raw_hyps, list) or not raw_hyps:
            return False
        stmts: list[str] = []
        for h in raw_hyps:
            try:
                if isinstance(h, dict):
                    s = _norm_statement(h.get("statement") or h.get("text"))
                else:
                    s = _norm_statement(h)
                if s:
                    stmts.append(s)
            except Exception:
                continue
        if not stmts:
            return False
        raw_claims = data.get("claims") or []
        if not isinstance(raw_claims, list) or not raw_claims:
            return False
        by_stmt: dict[str, list[str]] = {}
        for c in raw_claims:
            try:
                if not isinstance(c, dict):
                    continue
                s = _norm_statement(c.get("statement"))
                if not s:
                    continue
                by_stmt.setdefault(s, []).append(str(c.get("claim_kind") or "").upper())
            except Exception:
                continue
        if not by_stmt:
            return False
        for s in stmts:
            kinds = by_stmt.get(s)
            if not kinds:
                return False
            for k in kinds:
                if k != "NOT_CHECKABLE":
                    return False
        return True
    except Exception:
        return False


def _recent_collection_surfaces(
    action_history: list | None, limit: int = 10, *, separate_carriers: bool = False
) -> list[str]:
    """Kind-qualified surfaces collected in recent turns (pure).

    File reads contribute ``file:<path>`` (from action parameters
    ``file_path``); symbol resolutions contribute ``symbol:<name>``
    (from ``symbol_name``). Kind-qualified so a collected file does not
    damp a symbol pivot on the same token and vice versa. Bounded to
    the trailing window; never raises.
    """
    try:
        source = turn_entries(action_history) if separate_carriers else list(action_history or [])
        entries = source[-max(1, int(limit or 10)) :]
    except Exception:
        return []
    surfaces: list[str] = []
    for entry in entries:
        try:
            if not isinstance(entry, dict):
                continue
            params = entry.get("parameters") or {}
            if not isinstance(params, dict):
                continue
            file_path = str(params.get("file_path") or "").strip()
            if file_path:
                surfaces.append(f"file:{file_path}")
            symbol_name = str(params.get("symbol_name") or "").strip()
            if symbol_name:
                surfaces.append(f"symbol:{symbol_name}")
        except Exception:
            continue
    return surfaces


def _order_id(kind: str, action: str, params: dict[str, Any], seed_source: str) -> Any:
    """Deterministic work-order identity (pure).

    Advisory orders recompute every turn from the same history; uuid4
    would make each recomputation a distinct order and break replay
    determinism. uuid5 over the canonical content keeps emission
    idempotent: same signals, same IDs, safe to deduplicate downstream.
    Never raises.
    """
    try:
        canonical = "|".join(
            [
                str(WORK_ORDER_VERSION_CURRENT or ""),
                str(kind or ""),
                str(action or ""),
                json.dumps(dict(params or {}), sort_keys=True, default=str),
                str(seed_source or ""),
            ]
        )
    except Exception:
        canonical = f"{WORK_ORDER_VERSION_CURRENT}|{kind}|{action}|{seed_source}"
    try:
        return uuid.uuid5(uuid.NAMESPACE_URL, canonical)
    except Exception:
        return uuid.uuid4()


def _flat_scalar_params(params: Any) -> dict[str, Any] | None:
    """Carry-shaped parameters: a dict of JSON scalars only (pure).

    Mirrors the shape check the G3 slot enforces so carried REQUERY
    orders carry exactly what the cascade can dispatch. Returns the
    params dict, or None when the shape is not dispatchable.
    Never raises.
    """
    try:
        if not isinstance(params, dict):
            return None
        for key, value in params.items():
            if not isinstance(key, str):
                return None
            if value is not None and not isinstance(value, (str, int, float, bool)):
                return None
        return dict(params)
    except Exception:
        return None


def orders_from_cascade_signals(
    history: list[Any] | None,
    pivot: dict[str, Any] | None,
    triage_route: str | None,
) -> list[WorkOrder]:
    """Typed work orders from legacy signals (2b step 3, advisory, pure).

    Converts what the loop already banks into the typed layer without
    changing dispatch — candidate lifecycle stays separate from order
    consumption (steps 4+):
    - a fired pivot recollect → FETCH_SEED for the new surface.
    REQUERY orders are no longer emitted here: since step 5 the verify
    turn carries them directly (``_carry_requery_order``) and the G3
    slot dispatches from the carriers. GAP_FILL orders are no longer
    emitted either: since step 6 the verify turn carries them
    (``_carry_gap_orders``) and the smallest-fetch slot dispatches from
    the carrier. Deterministic IDs (content-addressed); PENDING status
    throughout; never raises.
    """
    orders: list[WorkOrder] = []
    try:
        route = str(triage_route or "").strip() or "UNKNOWN"
    except Exception:
        route = "UNKNOWN"

    def _character(claim_kind: str = "untyped") -> FailureCharacter:
        try:
            kind = str(claim_kind or "").strip().upper() or "UNTYPED"
        except Exception:
            kind = "UNTYPED"
        return FailureCharacter(triage_route=route[:32], claim_kind=kind[:64])

    # Gap orders arrive via carriers and dispatch from the
    # smallest-fetch slot — emission ignores them. Only the pivot
    # signal is emitted here.

    # Fired pivot → FETCH_SEED for the new surface.
    try:
        pivot = pivot if isinstance(pivot, dict) else {}
        surface = str(pivot.get("surface") or "").strip()
        kind = str(pivot.get("surface_kind") or "").strip() or "file"
        params = _flat_scalar_params(pivot.get("action_parameters"))
        if surface and params is not None:
            action_type = str(pivot.get("next_action") or "").upper() or (
                "FIND_SYMBOL" if kind == "symbol" else "GET_CODE"
            )
            seed_source = f"pivot:{kind}:{surface}"[:256]
            orders.append(
                WorkOrder(
                    work_order_id=_order_id("FETCH_SEED", action_type, params, seed_source),
                    kind=WorkOrderKind.FETCH_SEED,
                    action_type=action_type,
                    action_parameters=params,
                    addresses=_character(),
                    cost_estimate=1,
                    seed_source=seed_source,
                    status=WorkOrderStatus.PENDING,
                )
            )
    except Exception:
        pass
    return orders


def rank_advisory_orders(
    orders: list[WorkOrder] | None,
    history: list[Any] | None,
    banked_total: int,
) -> dict[str, Any]:
    """Rank advisory work orders best-first without dispatching (2b step 4, pure).

    Translates each PENDING order to frontier seeds with its own
    addresses as the failure character, then orders by base_score ×
    novelty. The novelty baseline derives from banked GET reads in
    history (attempted-but-unbanked rows do not count — advisory novelty
    is conservative by construction). Evidence state is coarse
    (row-count only; the loop owns richer bank state in step 5+).

    Deterministic: candidate IDs derive from order IDs, so ties break
    identically every turn. Returns ``{"evidence_state", "ranked",
    "skipped"}`` — ranked entries carry rank/identity/fetch/dimension/
    base_score; skipped holds order-ID hexes translation refused
    (unlocatable, over-cap, non-pending). Never raises.
    """
    ranked: list[dict[str, Any]] = []
    skipped: list[str] = []
    try:
        pending = [o for o in (orders or []) if isinstance(o, WorkOrder)]
    except Exception:
        return {"evidence_state": "UNKNOWN", "ranked": ranked, "skipped": skipped}
    try:
        banked = int(banked_total or 0)
    except (TypeError, ValueError):
        banked = 0
    evidence_state = (
        FrontierEvidenceState.NO_ROWS if banked <= 0 else FrontierEvidenceState.ROWS_WITHOUT_MATCH
    )
    try:
        collected: list[tuple[str | None, str | None]] = []
        for entry in history or []:
            try:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("action", "") or "").upper() not in ("GET_CODE", "GET_SOURCE"):
                    continue
                if int(entry.get("evidence_added", 0) or 0) <= 0:
                    continue
                params = entry.get("parameters") or {}
                if not isinstance(params, dict):
                    continue
                file_path = str(params.get("file_path") or "").strip() or None
                try:
                    start = int(params.get("start_line") or 0) or None
                except (TypeError, ValueError):
                    start = None
                try:
                    end = int(params.get("end_line") or 0) or None
                except (TypeError, ValueError):
                    end = None
                span: str | None = None
                if start is not None and end is not None:
                    span = f"{start}-{end}"
                elif start is not None:
                    span = str(start)
                collected.append((file_path, span))
            except Exception:
                continue
    except Exception:
        collected = []
    try:
        remaining = int(MAX_WORK_ORDER_SEEDS)
    except Exception:
        remaining = 8
    seeds_all: list[Any] = []
    order_of: dict[str, str] = {}
    try:
        for order in pending:
            try:
                if remaining <= 0:
                    skipped.append(order.work_order_id.hex)
                    continue
                pinned = {
                    order.work_order_id.hex: uuid.uuid5(
                        uuid.NAMESPACE_URL, f"frontier-candidate|{order.work_order_id.hex}"
                    )
                }
                seeds, order_skipped = work_orders_to_seeds(
                    [order],
                    order.addresses,
                    evidence_state,
                    max_seeds=remaining,
                    candidate_ids=pinned,
                )
                skipped.extend([s.hex if hasattr(s, "hex") else str(s) for s in order_skipped])
                remaining -= len(seeds)
                for seed in seeds:
                    order_of[seed.candidate_id.hex] = order.work_order_id.hex
                seeds_all.extend(seeds)
            except Exception:
                try:
                    skipped.append(order.work_order_id.hex)
                except Exception:
                    pass
                continue
    except Exception:
        pass
    try:
        ordered = order_candidates(seeds_all, collected)
    except Exception:
        ordered = []
    try:
        for position, seed in enumerate(ordered, start=1):
            try:
                ranked.append(
                    {
                        "rank": position,
                        "order_id": order_of.get(seed.candidate_id.hex, ""),
                        "seed_source": seed.seed_source,
                        "file_path": seed.file_path,
                        "span": seed.span,
                        "fetch_kind": seed.seed_fetch.fetch_kind,
                        "target": seed.seed_fetch.target,
                        "dimension": str(seed.dimension.value),
                        "base_score": seed.base_score,
                    }
                )
            except Exception:
                continue
    except Exception:
        pass
    return {"evidence_state": str(evidence_state.value), "ranked": ranked, "skipped": skipped}


@dataclass
class PostReasonCascadeState:
    """Inputs and outputs of the post-reason gathering cascade (2b seam).

    The workflow builds one per turn from activity results and loop
    counters, runs :func:`apply_post_reason_cascade`, and writes the
    outputs back. Plain data — JSON-safe except ``knowledge_seed``,
    which passes through untouched. Deterministic: no I/O, no clock.
    """

    reason_data: dict[str, Any]
    action_history: list
    evidence_banked_total: int
    triage_route: str | None
    description: str | None
    knowledge_seed: dict | None
    max_iterations: int
    iteration: int
    prev_focus: dict | None
    is_defer: bool
    d6_fired: bool
    absence_forced: bool
    counts: dict[str, int] = field(default_factory=dict)
    formulated_count: int = 0
    tenant_id: str = ""
    investigation_id: str = ""
    #: Loop-semantics pin recorded on the investigation (see
    #: ``domain.investigation.loop_version``). Routes version-pinned
    #: rule changes; unmarked callers get the current semantics.
    loop_version: str = LOOP_VERSION_CURRENT
    #: Carrier entry of the dispatched G3 requery order (2b step 5),
    #: for the workflow's consumed stamp. None when no order fired.
    consumed_order_ref: dict | None = None
    #: Advisory work orders as JSON dicts (2b step 3): recorded only,
    #: never dispatched by the cascade.
    advisory_orders: list = field(default_factory=list)
    #: Advisory frontier ranking (2b step 4): recorded only, never
    #: dispatched by the cascade.
    advisory_ranking: dict = field(default_factory=dict)


def apply_post_reason_cascade(state: PostReasonCascadeState) -> PostReasonCascadeState:
    """Run the real post-reason gathering cascade (2b seam, pure).

    Verbatim home of the cascade previously inline in
    RunInvestigationWorkflow: pivot recollect, stack-walk, ledger
    requery, evidence floor, formulate, synthesis, re-read, call-site,
    log search, replan, and the smallest-fetch slot, in that precedence
    order, followed by the turn-focus record. Later gathering rules
    stand down once ``forced_pivot_recollect`` is set — only the
    explicit-frame walk-stack (intake frames outrank everything) and
    the pre-cascade defer/D6/absence outcomes may override a pivot.
    Mutates and returns ``state``. Never raises.
    """
    try:
        _is_defer = bool(state.is_defer)
        _d6_fired = bool(state.d6_fired)
        _absence_forced = bool(state.absence_forced)
        _reason_data = dict(state.reason_data or {})
        # F7: work on a copy and commit once at the end. A fault in any rule
        # must not leave counters burned (or carriers stamped consumed)
        # without a dispatch, so every in-cascade write goes to these
        # locals and reaches ``state`` only after the whole cascade ran.
        _counts = dict(state.counts) if isinstance(state.counts, dict) else {}
        _pending_consume: list[dict[str, Any]] = []
        _history = list(state.action_history or [])
        # F2 (lv6): positional "recent turns" readers skip work-order carriers.
        _separate = _carriers_separated(state.loop_version)

        def _count(key: str) -> int:
            try:
                return int(_counts.get(key, 0) or 0)
            except (TypeError, ValueError):
                return 0

        # P6 2b-i: pivot recollect — the reasoner's point of focus
        # moved surfaces, so go back to collection on the new focus
        # instead of verifying or concluding on the stale one. Runs
        # before the other gathering forces so they don't gather on
        # the old focus; explicit-frame walk-stack below still wins
        # (intake frames outrank everything). Skipped on
        # defer/D6/absence turns, and the rule itself damps refires
        # on recently collected surfaces. Budget-blind by design
        # (agentic layers own budget); the turns-remaining gate is
        # liveness, not budget.
        _pivot_override = None
        if not _is_defer and not _d6_fired and not _absence_forced:
            _pivot_targets = _recent_collection_surfaces(_history, separate_carriers=_separate)
            _pivot_override = _should_force_pivot_recollect(
                state.prev_focus,
                _reason_data,
                int(state.max_iterations or 0) - int(state.iteration or 0),
                _pivot_targets,
            )
        if _pivot_override is not None:
            _reason_data = dict(_reason_data)
            _reason_data["next_action"] = _pivot_override["next_action"]
            _reason_data["action_parameters"] = _pivot_override["action_parameters"]
            _reason_data["forced_pivot_recollect"] = True
        # D2: stack-walk forcing first — explicit intake frames outrank
        # every other gathering rule.
        _walk_params = (
            None
            if _is_defer or _absence_forced or _d6_fired
            else _should_force_walk_stack(
                state.knowledge_seed, _reason_data, _history, _count("walk_stack")
            )
        )
        if _walk_params:
            _counts["walk_stack"] = _count("walk_stack") + 1
            _reason_data = dict(_reason_data)
            _reason_data["next_action"] = "WALK_STACK"
            _reason_data["action_parameters"] = _walk_params
            _reason_data["forced_walk_stack"] = True
            # F5: walk-stack overwrote the pivot's dispatch, so the pivot did
            # not happen; leaving the flag set would stamp history with a
            # pivot that never ran and damp its target as already collected.
            _reason_data["forced_pivot_recollect"] = False
        # G3: order-driven requery forcing (2b step 5) — a bounded
        # re-retrieval for an unsatisfied claim outranks the remaining
        # gathering rules but never stack-walk (evaluated only when no
        # walk-stack force fired this cascade). Carried REQUERY orders
        # are consumed newest-first with the same attempt-budget
        # matching markers used (same stamps, same summaries); only the
        # carrier changed. Same-turn summaries ride from history entries
        # via recorded_attempt_summaries, which carries each entry's
        # top-level attempt_id into the attempt dict.
        _consumed_order_ref: dict | None = None
        # G3 precedence: a fired stack-walk suppresses order
        # evaluation — carried orders never overwrite it.
        # F3: the slot stands down on defer and absence-disconfirmation
        # turns, like every other gathering rule — overwriting those forced
        # actions burned their budgets.
        _requery_owns_turn = not _is_defer and not _absence_forced
        if (
            _requery_owns_turn
            and not _reason_data.get("forced_walk_stack")
            and not _d6_fired
            and not _reason_data.get("forced_pivot_recollect")
        ):
            _recorded_summaries = recorded_attempt_summaries(_history or []) or []
            _order_override = _should_force_order_requery(_history, _recorded_summaries)
            if _order_override is not None and _order_override.get("mark_consumed"):
                _mark_idx = _order_override.get("order_index")
                try:
                    _mark_entry = (_history or [])[_mark_idx]
                    if isinstance(_mark_entry, dict):
                        # Stamp is committed only after the cascade succeeds.
                        _pending_consume.append(_mark_entry)
                        _consumed_order_ref = None
                except (TypeError, IndexError, KeyError):
                    pass
                _order_override = None
            if _order_override is not None:
                _reason_data = dict(_reason_data)
                _reason_data["next_action"] = _order_override["action_type"]
                _reason_data["action_parameters"] = _order_override["action_parameters"]
                _reason_data["forced_requery"] = True
                try:
                    _consumed_order_ref = (_history or [])[_order_override["order_index"]]
                except (TypeError, IndexError, KeyError):
                    _consumed_order_ref = None
                if not isinstance(_consumed_order_ref, dict):
                    _consumed_order_ref = None
        # Evidence floor — a committal proposal (formulate/verify/
        # conclude) on fewer than 3 banked rows for a code-shaped
        # incident gathers first instead of committing to a thin
        # theory. Skipped once explicit frames already forced.
        # Union of both lines' guards: skip floor forcing on defer turns
        # and once absence-forcing fired (L2-D3-fix), and once walk-stack
        # or requery forcing fired (g4-closure). No pivot guard: the floor
        # triggers only on committal proposals (FORMULATE/VERIFY/CONCLUDE)
        # while a pivot needs a fetch proposal — disjoint by construction
        # (see PIVOT_FILE_ACTIONS), pinned by test.
        if (
            not _is_defer
            and not _absence_forced
            and not _d6_fired
            and not _reason_data.get("forced_walk_stack")
            and not _reason_data.get("forced_requery")
        ):
            _floor = _explore_floor_target(
                _reason_data,
                int(state.evidence_banked_total or 0),
                state.triage_route,
                state.description,
                state.knowledge_seed,
                _count("floor"),
                history=_history,
            )
            if _floor:
                _counts["floor"] = _count("floor") + 1
                _reason_data = dict(_reason_data)
                _reason_data["next_action"] = _floor["next_action"]
                _reason_data["action_parameters"] = _floor["action_parameters"]
                _reason_data["forced_floor"] = True
        if (
            not _is_defer
            and not _absence_forced
            and not _d6_fired
            and _should_force_formulate(
                int(state.iteration or 0),
                int(state.formulated_count or 0),
                _reason_data.get("hypotheses_evaluated"),
                int(state.evidence_banked_total or 0),
                _count("formulate"),
            )
            and not _reason_data.get("forced_walk_stack")
            and not _reason_data.get("forced_floor")
            and not _reason_data.get("forced_requery")
            and not _reason_data.get("forced_pivot_recollect")
        ):
            _counts["formulate"] = _count("formulate") + 1
            _reason_data = dict(_reason_data)
            _reason_data["next_action"] = "FORMULATE_HYPOTHESIS"
            _reason_data["action_parameters"] = {}
            _reason_data["forced_formulate"] = True
        # T2-4: synthesis forcing — rich multi-action corpus with
        # nothing committed gets FORMULATE (same override as G8).
        if (
            not _is_defer
            and not _absence_forced
            and not _d6_fired
            and not _reason_data.get("forced_walk_stack")
            and not _reason_data.get("forced_formulate")
            and not _reason_data.get("forced_floor")
            and not _reason_data.get("forced_requery")
            and not _reason_data.get("forced_pivot_recollect")
        ):
            if _should_force_synthesis(
                _history,
                int(state.evidence_banked_total or 0),
                int(state.formulated_count or 0),
                _count("synthesis"),
                _reason_data,
            ):
                _counts["synthesis"] = _count("synthesis") + 1
                _reason_data = dict(_reason_data)
                _reason_data["next_action"] = "FORMULATE_HYPOTHESIS"
                _reason_data["action_parameters"] = {}
                _reason_data["forced_formulate"] = True
        # P0-2: re-read forcing — definition claim heading to VERIFY
        # without a range-read gets the read first (G3 floor would
        # INCONCLUSIVE the verify). Skipped when G8/D2 already forced.
        # No pivot guard: the re-read triggers only on VERIFY proposals
        # while a pivot needs a fetch proposal — disjoint by construction
        # (see PIVOT_FILE_ACTIONS), pinned by test.
        if (
            not _is_defer
            and not _absence_forced
            and not _d6_fired
            and not _reason_data.get("forced_formulate")
            and not _reason_data.get("forced_walk_stack")
            and not _reason_data.get("forced_floor")
            and not _reason_data.get("forced_requery")
        ):
            _reread_path = _reread_target(_reason_data, _history, _count("reread"))
            if _reread_path:
                _counts["reread"] = _count("reread") + 1
                _reason_data = dict(_reason_data)
                _reason_data["next_action"] = "GET_CODE"
                _reason_data["action_parameters"] = {"file_path": _reread_path}
                _reason_data["forced_reread"] = True
        # Call-site forcing — a CALL_EDGE claim naming an unread caller
        # file reads it before the loop can conclude on a graph summary
        # alone. Skipped when an earlier read-forcing rule already fired.
        if (
            not _is_defer
            and not _absence_forced
            and not _d6_fired
            and not _reason_data.get("forced_formulate")
            and not _reason_data.get("forced_walk_stack")
            and not _reason_data.get("forced_floor")
            and not _reason_data.get("forced_reread")
            and not _reason_data.get("forced_requery")
            and not _reason_data.get("forced_pivot_recollect")
        ):
            _callsite = _callsite_target(
                _reason_data,
                _history,
                _count("callsite"),
                tenant_id=state.tenant_id,
                investigation_id=str(state.investigation_id),
            )
            if _callsite:
                _counts["callsite"] = _count("callsite") + 1
                _reason_data = dict(_reason_data)
                _reason_data["next_action"] = _callsite["next_action"]
                _reason_data["action_parameters"] = _callsite["action_parameters"]
                _reason_data["forced_callsite"] = True
        # B-4: log-first forcing — service-error complaints gather logs
        # before code/state. Skipped when G8/G3/D2 already forced.
        if (
            not _is_defer
            and not _absence_forced
            and not _reason_data.get("forced_formulate")
            and not _reason_data.get("forced_reread")
            and not _reason_data.get("forced_walk_stack")
            and not _reason_data.get("forced_floor")
            and not _d6_fired
            and not _reason_data.get("forced_callsite")
            and not _reason_data.get("forced_requery")
            and not _reason_data.get("forced_pivot_recollect")
        ):
            _log_params = _should_force_log_search(
                str(state.description or ""),
                _reason_data,
                _history,
                _count("log_search"),
            )
            if _log_params:
                _counts["log_search"] = _count("log_search") + 1
                _reason_data = dict(_reason_data)
                _reason_data["next_action"] = "SEARCH_LOGS"
                _reason_data["action_parameters"] = _log_params
                _reason_data["forced_log_search"] = True
        # E3: replan trigger last — two enriched empties on one surface
        # plus same proposal redirects to the alternative family.
        # Skipped when any earlier rule already forced.
        if (
            not _is_defer
            and not _absence_forced
            and not _reason_data.get("forced_formulate")
            and not _reason_data.get("forced_reread")
            and not _reason_data.get("forced_log_search")
            and not _reason_data.get("forced_walk_stack")
            and not _reason_data.get("forced_floor")
            and not _d6_fired
            and not _reason_data.get("forced_callsite")
            and not _reason_data.get("forced_requery")
            and not _reason_data.get("forced_pivot_recollect")
        ):
            _replan = _should_force_replan(
                str(state.description or ""),
                _reason_data,
                _history,
                _count("replan"),
                separate_carriers=_separate,
            )
            if _replan:
                _counts["replan"] = _count("replan") + 1
                _reason_data = dict(_reason_data)
                _reason_data["next_action"] = _replan["next_action"]
                _reason_data["action_parameters"] = _replan["action_parameters"]
                _reason_data["forced_replan"] = True
        # Phase 4 (claim binding): smallest-fetch slot — LAST override
        # before the story-debug/shadow capture, so no later rule can
        # clobber it. First kind with an open order and a dispatchable
        # directive wins the turn. Orders arrive via carriers (newest
        # carrier only — an orders-empty carrier supersedes older ones);
        # coverage stamps prevent refires, so no in-place consumption.
        if (
            not _is_defer
            and not _absence_forced
            and not _reason_data.get("forced_formulate")
            and not _reason_data.get("forced_reread")
            and not _reason_data.get("forced_walk_stack")
            and not _reason_data.get("forced_floor")
            and not _reason_data.get("forced_callsite")
            and not _reason_data.get("forced_requery")
            and not _reason_data.get("forced_log_search")
            and not _reason_data.get("forced_replan")
            and not _reason_data.get("forced_not_checkable")
            and not _reason_data.get("forced_pivot_recollect")
        ):
            _carrier_orders: list[dict[str, Any]] = []
            try:
                for _evt in reversed(_history or []):
                    if isinstance(_evt, dict) and _evt.get("kind") == "work_order_feed":
                        _raw_orders = _evt.get("orders")
                        if isinstance(_raw_orders, list):
                            _carrier_orders = [o for o in _raw_orders if isinstance(o, dict)]
                        break
            except Exception:
                _carrier_orders = []
            _picked_order = None
            _fetch_kind = ""
            for _fetch_kind in SMALLEST_FETCH_ORDER:
                try:
                    _counter = {
                        "DEFINITION": _count("smallest_fetch_definition"),
                        "HISTORY": _count("smallest_fetch_history"),
                        "BEHAVIOR": _count("smallest_fetch_behavior"),
                        "ABSENCE": _count("smallest_fetch_absence"),
                        "CAUSAL": _count("smallest_fetch_causal"),
                    }[_fetch_kind]
                except Exception:
                    continue
                try:
                    _picked_order = _pick_gap_order(
                        _carrier_orders,
                        kind=_fetch_kind,
                        history=_history,
                        forced_count=_counter,
                    )
                except Exception:
                    _picked_order = None
                if _picked_order is not None:
                    break
            if _picked_order is not None:
                try:
                    if _fetch_kind == "DEFINITION":
                        _counts["smallest_fetch_definition"] = (
                            _count("smallest_fetch_definition") + 1
                        )
                    elif _fetch_kind == "HISTORY":
                        _counts["smallest_fetch_history"] = _count("smallest_fetch_history") + 1
                    elif _fetch_kind == "BEHAVIOR":
                        _counts["smallest_fetch_behavior"] = _count("smallest_fetch_behavior") + 1
                    elif _fetch_kind == "ABSENCE":
                        _counts["smallest_fetch_absence"] = _count("smallest_fetch_absence") + 1
                    else:
                        _counts["smallest_fetch_causal"] = _count("smallest_fetch_causal") + 1
                except Exception:
                    pass
                _reason_data = dict(_reason_data)
                _reason_data["next_action"] = _picked_order.action_type
                _reason_data["action_parameters"] = dict(_picked_order.action_parameters or {})
                _reason_data["forced_smallest_fetch"] = True
                _reason_data["forced_smallest_fetch_kind"] = _fetch_kind
                _reason_data["forced_smallest_fetch_gap_id"] = str(_picked_order.gap_id or "")
                # Workflow-only bank context (D4): identity travels
                # beside params, never inside them.
                try:
                    _reason_data["smallest_fetch_context"] = {
                        "kind": str(_fetch_kind),
                        "gap_id": str(_picked_order.gap_id or ""),
                        "claim_ref": str(_picked_order.claim_ref or ""),
                    }
                except Exception:
                    pass
        # P6 2b-i: record this turn's focus for next turn's pivot
        # comparison. Post-cascade read is deliberate: focus names the
        # fetch the loop actually dispatched, so pivot seeds and
        # damping anchor on collected reality, not the reasoner's
        # proposal. Proposal drift stays visible — next turn
        # detect_shift compares the new proposal against this
        # dispatched surface. Plain JSON scalars, replay-safe.
        _turn_focus = None
        try:
            _turn_focus = focus_from_reason_data(
                _reason_data if isinstance(_reason_data, dict) else {}
            )
            if _turn_focus is not None:
                state.prev_focus = _turn_focus.model_dump(mode="json")
        except Exception:
            pass
        # 2b step 3: advisory work-order emission — typed orders from
        # legacy signals, recorded only. Candidate lifecycle stays
        # separate from order consumption: orders are PENDING and
        # nothing here dispatches them (steps 4+ rank and consume).
        try:
            _pivot_surface: dict[str, Any] | None = None
            if _pivot_override is not None and _turn_focus is not None:
                _pivot_surface = {
                    "surface": _turn_focus.surface,
                    "surface_kind": _turn_focus.surface_kind,
                    "next_action": str(_pivot_override.get("next_action") or ""),
                    "action_parameters": _pivot_override.get("action_parameters"),
                }
            _advisory_models = orders_from_cascade_signals(
                _history, _pivot_surface, state.triage_route
            )
            state.advisory_orders = [order.model_dump(mode="json") for order in _advisory_models]
        except Exception:
            _advisory_models = []
            state.advisory_orders = []
        # 2b step 4: advisory frontier ranking — translate the recorded
        # orders to seeds and order them AFTER the cascade, recording the
        # recommendation without changing dispatch.
        try:
            state.advisory_ranking = rank_advisory_orders(
                _advisory_models,
                _history,
                int(state.evidence_banked_total or 0),
            )
        except Exception:
            state.advisory_ranking = {}
        # F7 commit point: the whole cascade ran, so counters and consume
        # stamps become visible together with the chosen dispatch.
        for _stamped in _pending_consume:
            _stamped["consumed"] = True
        if isinstance(state.counts, dict):
            state.counts.clear()
            state.counts.update(_counts)
        state.reason_data = _reason_data
        state.consumed_order_ref = _consumed_order_ref
        return state
    except Exception as exc:
        # Never raises into the workflow, but never silently either: the
        # proposal passes through untouched (counters/stamps uncommitted).
        logger.warning(
            "post-reason cascade fault; proposal passes through uncommitted: %s",
            type(exc).__name__,
            extra={
                "event": "cascade fault",
                "tenant_id": getattr(state, "tenant_id", ""),
                "investigation_id": getattr(state, "investigation_id", ""),
                "error": type(exc).__name__,
            },
        )
        return state
