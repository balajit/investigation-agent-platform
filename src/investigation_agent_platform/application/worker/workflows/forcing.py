# src/investigation_agent_platform/application/worker/workflows/forcing.py
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, NewType

from temporalio import workflow

from investigation_agent_platform.application.worker.reasoning_failure import (
    TRANSIENT_FAILURE_TYPE,
)

logger = logging.getLogger(__name__)

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
    from investigation_agent_platform.domain.investigation.forcing_rules import (
        RULES,
        Force,
        ForcePayload,
    )
    from investigation_agent_platform.domain.investigation.loop_version import (
        LOOP_VERSION_CURRENT,
        LOOP_VERSION_LV4,
        LOOP_VERSION_LV6,
        LOOP_VERSION_LV7,
        LOOP_VERSION_LV8,
        LOOP_VERSION_LVW,
        loop_version_at_least,
    )

with workflow.unsafe.imports_passed_through():
    from investigation_agent_platform.domain.collection.focus import (
        FocusPoint,
        FocusShift,
        canonical_surface,
        detect_shift,
        focus_from_reason_data,
        pivot_can_pay_off,
    )
    from investigation_agent_platform.domain.collection.work_orders import (
        KNOWN_WORK_ORDER_VERSIONS,
        FailureCharacter,
        WorkOrder,
        WorkOrderKind,
        WorkOrderStatus,
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
    forced_bits = [force.label for force in Force if reason_data.get(force.value)] if isinstance(reason_data, dict) else []
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


class ConsumeReason(StrEnum):
    """Why a REQUERY carrier was retired without dispatching (W2-D3, closed set)."""

    BUDGET_EXHAUSTED = "budget_exhausted"  # directive_consumed: attempts spent or stamp matched
    EXPIRED = "expired"  # LV_W: age in turns exceeded the order's ttl_rounds
    INVALID_ORDER = "invalid_order"  # payload missing/malformed/wrong kind/not PENDING/unknown version


def _safe_int(value: Any) -> int | None:
    """int(value), or None for a boolean, a non-number or an overflow (pure)."""
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _work_orders_hardened(loop_version: Any) -> bool:
    """Whether the run's pin carries the LV_W work-order semantics (fail-safe False).

    Same shape as ``_pivot_hardened`` (lv7): an unrecognized or unreadable pin keeps
    the old behavior.
    """
    try:
        return bool(loop_version_at_least({"loop_version": loop_version}, LOOP_VERSION_LVW))
    except Exception:
        return False


@dataclass(frozen=True)
class WorkOrderPolicy:
    """What the carrier readers may assume this turn (pure value, JSON-free).

    ``hardened`` False reproduces the lv7 reads exactly: scan all history for the
    newest unconsumed REQUERY carrier, no expiry, stamp-only gap coverage.
    ``iteration`` is the cascade's current turn, used to age carriers; None when the
    turn counter is unreadable, which makes every age unreadable (carriers fail closed).
    """

    hardened: bool = False
    iteration: int | None = None


def _carrier_age(entry: dict[str, Any], iteration: int | None) -> int | None:
    """Age in turns of a carrier (pure, never raises; None when unreadable)."""
    try:
        if iteration is None:
            return None
        turn_issued = _safe_int(entry.get("turn_issued", entry.get("iteration")))
        if turn_issued is None:
            return None
        return int(iteration) - int(turn_issued)
    except Exception:
        return None


def _retirement_event(
    entry: dict[str, Any], reason: ConsumeReason, decision: dict[str, Any], policy: WorkOrderPolicy
) -> dict[str, Any]:
    """One order_events entry for a retired carrier (pure, never raises)."""
    try:
        return {
            "event": "requery_order_retired",
            "reason": reason.value,
            "detail": str(decision.get("detail") or ""),
            "claim_ref": str(entry.get("claim_ref") or ""),
            "turn_issued": _safe_int(entry.get("turn_issued", entry.get("iteration"))),
            "age": _carrier_age(entry, policy.iteration),
        }
    except Exception:
        return {"event": "requery_order_retired", "reason": reason.value, "detail": ""}


def _invalid_order_detail(carrier: dict[str, Any]) -> str | None:
    """Why a REQUERY carrier's order cannot dispatch, or None when it can (pure, never raises).

    Codes: ``not_a_dict`` (payload missing or wrong type), ``validation_failed`` (the WorkOrder
    model rejects it), ``unknown_version`` (a version this build does not understand),
    ``wrong_kind`` (a valid order that is not REQUERY), ``not_pending`` (status CONSUMED or
    EXPIRED on a carrier still marked unconsumed: inconsistent state).
    """
    try:
        payload = carrier.get("order")
        if not isinstance(payload, dict):
            return "not_a_dict"
        try:
            order = WorkOrder.model_validate(payload)
        except Exception:
            return "validation_failed"
        if order.version not in KNOWN_WORK_ORDER_VERSIONS:
            return "unknown_version"
        if order.kind != WorkOrderKind.REQUERY:
            return "wrong_kind"
        if order.status != WorkOrderStatus.PENDING:
            return "not_pending"
        return None
    except Exception:
        return "validation_failed"


#: Transient infrastructure misses: a stamped smallest-fetch turn that ended this way
#: may be retried once (W3-D16). Anything else deterministic is covered, never retried.
_TRANSIENT_MISS_REASONS = frozenset(
    {"timeout", "provider_unavailable", "rate_limited", "idempotency_owner_busy"}
)


def _fetch_retryable(entry: dict[str, Any]) -> bool:
    """Whether a stamped smallest-fetch turn ended in a failure worth one retry (pure)."""
    try:
        status = str(entry.get("outcome_status") or "").upper()
        if status == "FAILED":
            return True
        if status == "DEGRADED":
            return str(entry.get("miss_reason") or "") in _TRANSIENT_MISS_REASONS
        return False
    except Exception:
        return False


def _gap_coverage(
    action_history: list[Any] | None, gap_id: str, reason: str = ""
) -> tuple[bool, int]:
    """(covered, attempts) for one gap identity (gap_id, reason) under LV_W (pure, never raises).

    ``attempts`` counts the history entries stamped for this identity. The gap is covered
    when any such entry ended in a non-retryable outcome, or when the attempts reached
    ``GAP_MAX_ATTEMPTS``. An entry stamped with a different reason is a different identity.
    """
    try:
        want, want_reason = str(gap_id or ""), str(reason or "")
        if not want:
            return False, 0
        attempts, covered = 0, False
        for entry in action_history or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("forced_smallest_fetch_gap_id") or "") != want:
                continue
            if str(entry.get("forced_smallest_fetch_reason") or "") != want_reason:
                continue
            attempts += 1
            if not _fetch_retryable(entry):
                covered = True
        return (covered or attempts >= GAP_MAX_ATTEMPTS), attempts
    except Exception:
        return True, 0  # fail toward no refire


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


#: A history view that is safe to index positionally ("the last N turns"). Build it only with
#: ``turn_view``; the carrier readers use the full list instead (they index into it and stamp in place).
Turns = NewType("Turns", list)


def turn_view(history: Any, loop_version: Any) -> Turns:
    """The turn entries a positional reader may index (pure, never raises).

    From lv6 carriers are removed; an older, unreadable or unknown pin keeps the raw dict entries,
    so carriers read as turns exactly as those runs always did (review F2, accepted by design).
    """
    try:
        if _carriers_separated(loop_version):
            return Turns(turn_entries(history))
        return Turns([e for e in (history or []) if isinstance(e, dict)])
    except Exception:
        return Turns([])


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


#: Per-run cap on the pivot recollect (lv7), matching the other forcing rules.
PIVOT_RECOLLECT_CAP = 2  # lv7
#: Set only after the D23 review returns raise or lower; the value applies from lv8.
PIVOT_RECOLLECT_CAP_NEXT: int | None = None


def pivot_cap_for(loop_version: Any) -> int:
    """Per-run pivot cap routed on the pin (F4-D24, pure, fail-safe).

    Today the next cap is unset, so every pin returns 2. Once the D23 review
    sets ``PIVOT_RECOLLECT_CAP_NEXT``, the new value applies from lv8 on;
    older pins and unreadable pins keep 2.
    """
    try:
        if (
            PIVOT_RECOLLECT_CAP_NEXT is not None
            and isinstance(loop_version, str)
            and loop_version.strip()
            and loop_version_at_least({"loop_version": loop_version}, LOOP_VERSION_LV8)
        ):
            return int(PIVOT_RECOLLECT_CAP_NEXT)
    except Exception:
        pass
    return PIVOT_RECOLLECT_CAP


def _pivot_hardened(loop_version: Any) -> bool:
    """Whether the run's pin uses the bounded, non-destructive pivot (lv7+).

    Fail-safe: an unrecognized or unreadable pin returns False (old pivot).
    """
    try:
        return bool(loop_version_at_least({"loop_version": loop_version}, LOOP_VERSION_LV7))
    except Exception:
        return False


def _proposed_params(reason_data: Any) -> dict[str, Any]:
    """A copy of the model's proposed action parameters ({} when malformed)."""
    try:
        raw = (reason_data or {}).get("action_parameters")
        return dict(raw) if isinstance(raw, dict) else {}
    except Exception:
        return {}


class PivotReason(StrEnum):
    """Why the pivot rule did or did not fire (F4-D22, observability only)."""

    FIRED = "fired"
    LIVENESS = "liveness"  # fewer than two turns left
    NO_SHIFT = "no_shift"  # no focus, no previous focus, same surface, or only a window refinement
    DAMPED = "damped"  # the surface was collected recently
    SCOPED_SEARCH = "scoped_search"  # lv7: a SEARCH_CODE that already carries a query is left alone
    CAPPED = "capped"  # lv7: the per-run cap withheld a pivot that would have fired


@dataclass(frozen=True)
class PivotDecision:
    """The pivot rule's output and its reason (pure)."""

    override: dict[str, Any] | None
    reason: PivotReason


#: A REQUERY order issued at verify turn N is first evaluated at turn N+1. The F4 pivot
#: may outrank it for at most PIVOT_RECOLLECT_CAP turns, so the order must survive
#: PIVOT_RECOLLECT_CAP + 1 turns of age to be dispatchable on the first turn the pivot
#: has stood down. (Owner decision 2026-10-31: the pivot outranks a waiting order.)
REQUERY_ORDER_TTL_TURNS = PIVOT_RECOLLECT_CAP + 1

#: A GAP_FILL feed is re-emitted by every healthy verify turn (age 1 on the next cascade).
#: Three turns tolerates one skipped verify (a defer turn) plus one fault turn, after which
#: the feed describes a hypothesis the loop has moved past.
GAP_ORDER_TTL_TURNS = 3

#: One initial fire plus one retry per (gap identity, reason).
GAP_MAX_ATTEMPTS = 2


def _cap_reached(forced_count: Any) -> bool:
    try:
        return not isinstance(forced_count, bool) and int(forced_count) >= PIVOT_RECOLLECT_CAP
    except (TypeError, ValueError, OverflowError):
        return False  # a malformed counter leaves the cap inert, as before


def _hardened_override(curr: Any, proposed: str, kept: dict[str, Any]) -> dict[str, Any]:
    """The existing lv7 non-destructive rewrite, unchanged (F4-D22)."""
    if curr.surface_kind == "file":
        kept["file_path"] = curr.surface
        if curr.line_start is not None and curr.line_start >= 1:
            kept["start_line"] = curr.line_start
        if curr.line_end is not None and curr.line_end >= 1:
            kept["end_line"] = curr.line_end
        kept["revision"] = str(kept.get("revision") or "").strip() or "HEAD"
        return {
            "next_action": proposed if proposed in ("GET_CODE", "GET_SOURCE") else "GET_CODE",
            "action_parameters": kept,
            "forced_pivot_recollect": True,
        }
    kept["symbol_name"] = curr.surface
    kept["revision"] = str(kept.get("revision") or "").strip() or "HEAD"
    return {
        "next_action": "FIND_SYMBOL",
        "action_parameters": kept,
        "forced_pivot_recollect": True,
    }


def _legacy_override(curr: Any) -> dict[str, Any]:
    """The existing pre-lv7 rewrite, unchanged (F4-D22)."""
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


def _canonical_recent(entry: str) -> str:
    kind, _, rest = entry.partition(":")
    return f"{kind}:{canonical_surface(rest, kind)}"


def _pivot_decision(
    prev_focus: dict[str, Any] | None,
    reason_data: dict[str, Any] | None,
    turns_remaining: int,
    recent_targets: list[str] | None,
    *,
    preserve_proposal: bool = False,
    forced_count: Any = 0,
    hardened: bool = False,
) -> PivotDecision:
    """The pivot rule with its reason (pure, never raises). Dispatch decisions equal the old rule's."""
    try:
        if not pivot_can_pay_off(turns_remaining):
            return PivotDecision(None, PivotReason.LIVENESS)
        prev: FocusPoint | None = None
        if isinstance(prev_focus, dict):
            try:
                prev = FocusPoint.model_validate(prev_focus)
            except Exception:
                prev = None
        curr = focus_from_reason_data(
            reason_data if isinstance(reason_data, dict) else {}, hardened=hardened
        )
        if curr is None or detect_shift(prev, curr, hardened=hardened) != FocusShift.PIVOTED:
            return PivotDecision(None, PivotReason.NO_SHIFT)
        recent = {str(r or "") for r in (recent_targets or []) if str(r or "")}
        if hardened:
            recent = {_canonical_recent(r) for r in recent}
            kind, surface = curr.identity
            key = f"{kind}:{surface}"
        else:
            key = f"{curr.surface_kind}:{curr.surface}"
        if key in recent:
            return PivotDecision(None, PivotReason.DAMPED)
        if preserve_proposal:
            proposed = str((reason_data or {}).get("next_action", "") or "").upper()
            kept = _proposed_params(reason_data)
            if (
                curr.surface_kind == "file"
                and proposed == "SEARCH_CODE"
                and str(kept.get("query") or "").strip()
            ):
                return PivotDecision(None, PivotReason.SCOPED_SEARCH)
            if _cap_reached(forced_count):
                return PivotDecision(None, PivotReason.CAPPED)
            return PivotDecision(_hardened_override(curr, proposed, kept), PivotReason.FIRED)
        return PivotDecision(_legacy_override(curr), PivotReason.FIRED)
    except Exception:
        return PivotDecision(None, PivotReason.NO_SHIFT)


def _should_force_pivot_recollect(
    prev_focus: dict[str, Any] | None,
    reason_data: dict[str, Any] | None,
    turns_remaining: int,
    recent_targets: list[str] | None,
    *,
    preserve_proposal: bool = False,
    forced_count: Any = 0,
    hardened: bool = False,
) -> dict[str, Any] | None:
    """Collection-forcing override on focus pivot (P6 2b-i, pure).

    Stable pure API with many callers: delegates to :func:`_pivot_decision`
    and returns the override (F4-D22). Never raises.
    """
    return _pivot_decision(
        prev_focus,
        reason_data,
        turns_remaining,
        recent_targets,
        preserve_proposal=preserve_proposal,
        forced_count=forced_count,
        hardened=hardened,
    ).override


def _carry_requery_order(
    verify_data: dict[str, Any] | None, iteration: int
) -> dict[str, Any] | None:
    """G3 order carrier to append to action_history, or None (requery carrier).

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
                "ttl_rounds": REQUERY_ORDER_TTL_TURNS,
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
    policy: WorkOrderPolicy | None = None,
) -> dict[str, Any] | None:
    """G3 order forcing decision (requery carrier: dispatch from carriers).

    Returns the typed override (``action_type``, ``action_parameters``,
    ``order_index``) for the dispatchable carried REQUERY order, or
    ``{"mark_consumed": <ConsumeReason value>, "detail": code,
    "order_index": index}`` when the carrier retires without dispatch, or
    None when the turn proceeds unchanged. Attempt-budget matching is
    unchanged (same stamps, same summaries). Reads only workflow history
    plus same-turn summaries. Pure — workflow-safe.

    Under ``hardened`` (LV_W, W2-D9/D10) only the newest REQUERY carrier is
    read (an older carrier never resurrects behind a consumed newest one)
    and a carrier older than its order's ``ttl_rounds`` expires. Older pins
    keep the scan-all-history read and no expiry. Invalid orders retire as
    ``invalid_order`` on every pin (W-INV: the label was a lie before).

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
    hardened = bool(getattr(policy, "hardened", False))
    iteration = getattr(policy, "iteration", None)
    candidate_index: int | None = None
    if hardened:
        for index in range(len(entries) - 1, -1, -1):
            entry = entries[index]
            if not isinstance(entry, dict):
                continue
            if entry.get("kind") != "work_order":
                continue
            if entry.get("order_kind") != WorkOrderKind.REQUERY.value:
                continue
            if entry.get("consumed"):
                return None  # a consumed newest carrier never resurrects an older one
            candidate_index = index
            break
        if candidate_index is None:
            return None
    else:
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
    detail = _invalid_order_detail(carrier)
    if detail is not None:
        return {
            "mark_consumed": ConsumeReason.INVALID_ORDER.value,
            "detail": detail,
            "order_index": candidate_index,
        }
    try:
        payload = carrier.get("order")
        order = WorkOrder.model_validate(payload) if isinstance(payload, dict) else None
    except Exception:
        order = None
    if order is None:  # validated above; fail closed
        return {
            "mark_consumed": ConsumeReason.INVALID_ORDER.value,
            "detail": "validation_failed",
            "order_index": candidate_index,
        }
    try:
        consumed = directive_consumed(carrier, entries, recorded)
    except Exception:
        return None
    if consumed:
        return {
            "mark_consumed": ConsumeReason.BUDGET_EXHAUSTED.value,
            "detail": "attempt_budget",
            "order_index": candidate_index,
        }
    if hardened:
        age = _carrier_age(carrier, iteration)
        if age is None:
            return {
                "mark_consumed": ConsumeReason.INVALID_ORDER.value,
                "detail": "unreadable_age",
                "order_index": candidate_index,
            }
        try:
            ttl = int(order.ttl_rounds)
        except Exception:
            ttl = REQUERY_ORDER_TTL_TURNS
        if age > ttl:
            return {
                "mark_consumed": ConsumeReason.EXPIRED.value,
                "detail": "ttl_exceeded",
                "order_index": candidate_index,
            }
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
    turns: Turns,
    forced_count: int,
) -> dict | None:
    """Alternative gathering action after repeated empties, or None.

    Fires when the two most recent enriched turns share one action with no
    evidence banked on either, and the current proposal repeats it. The
    production call site runs before the current action is appended, so
    the proposal arrives via reason_data (not history): history holds only
    completed enriched turns. Takes the typed ``Turns`` view (F2-D18), never
    a raw history: carriers cannot displace a turn. Returns {next_action,
    params} for the first alternative family, or None. Bounded (max 2 per
    run). Pure.
    """
    try:
        if int(forced_count or 0) >= 2:
            return None
        entries = [e for e in (turns or []) if isinstance(e, dict)]
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
    "CAUSAL": frozenset({"GET_CODE", "SEARCH_LOGS"}),
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


_NOTARGET_STOPWORDS = frozenset(
    {
        "the", "a", "an", "of", "to", "in", "is", "it", "and", "or", "for",
        "on", "with", "by", "from", "at", "as", "be", "are", "was", "were",
        "this", "that", "these", "those", "when", "where", "which", "what",
        "how", "why", "not", "no", "if", "then", "than", "into", "over",
        "under", "between", "through", "during", "before", "after", "db",
    }
)


def _notarget_keywords(query: str) -> list[str]:
    """Keyword tokens for a targetless gap query (pure, total, never raises)."""
    seen: list[str] = []
    try:
        for token in re.split(r"[^A-Za-z0-9_.]+", query or ""):
            low = token.lower()
            if len(low) >= 4 and low not in _NOTARGET_STOPWORDS and low not in seen:
                seen.append(low)
            if len(seen) >= 8:
                break
    except Exception:
        pass
    return seen


def _causal_fetch_params(gap: dict[str, Any]) -> dict[str, Any] | None:
    """GET_CODE params for a CAUSAL gap's link window (pure)."""
    try:
        _raw_target = gap.get("target")
        target: dict[str, Any] = _raw_target if isinstance(_raw_target, dict) else {}
        file_path = str(target.get("file_path", "") or "").strip()
        if not file_path:
            if gap.get("no_target"):
                keywords = _notarget_keywords(str(gap.get("query") or ""))
                if keywords:
                    return {"keywords": keywords, "limit": 10, "_leg": "SEARCH_LOGS"}
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
    """Directive action for a kind, honoring the BEHAVIOR/CAUSAL leg (pure)."""
    try:
        spec = KIND_FETCH.get(str(kind or ""))
        if not spec:
            return ""
        leg = str((params or {}).pop("_leg", "") or "")
        if leg in ("QUERY_STATE", "SEARCH_LOGS"):
            return leg
        return str(spec.get("action") or "")
    except Exception:
        return ""


def _pick_gap_order(
    orders: list[dict[str, Any]] | None,
    *,
    kind: str,
    history: list[Any] | None,
    forced_count: int,
    policy: WorkOrderPolicy | None = None,
    carrier_turn: int | None = None,
    allow_retry: bool = False,
) -> WorkOrder | None:
    """First dispatchable GAP_FILL order of one claim kind, or None (pure).

    Step-6 per-kind picker over a carrier's orders: kind match, gap not
    already covered, per-kind run cap, closed kind→action triple.
    Content-free by design: gap identity + history coverage decide.
    Never raises.

    Under a hardened policy the coverage is outcome-aware
    (``_gap_coverage`` on the (gap id, reason) identity, W3-D16/D17), a
    carrier older than the order's ``ttl_rounds`` is skipped, and retries
    (attempts > 0) are taken only when ``allow_retry`` (W3-D19: first
    fires before retries across kinds). Older pins keep stamp-only,
    one-fire-per-gap coverage.
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
        hardened = bool(getattr(policy, "hardened", False))
        iteration = getattr(policy, "iteration", None)
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
                if hardened:
                    try:
                        ttl = int(order.ttl_rounds)
                    except Exception:
                        ttl = GAP_ORDER_TTL_TURNS
                    age = _carrier_age({"turn_issued": carrier_turn}, iteration)
                    if age is not None and age > ttl:
                        continue
                    covered, attempts = _gap_coverage(history, gap_id, str(order.gap_reason or ""))
                    if covered:
                        continue
                    if attempts > 0 and not allow_retry:
                        continue
                # Covered gaps fall through to the next open order of
                # the same kind — a stale carrier never suppresses its
                # siblings.
                elif _smallest_fetch_already_covered(history, gap_id):
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
            ttl_rounds=GAP_ORDER_TTL_TURNS,
            gap_id=gap_id,
            gap_reason=str(gap.get("reason") or "") or None,
            claim_ref=str(gap.get("claim_ref") or "") or None,
            seed_source=f"gap:{gap_id}",
            status=WorkOrderStatus.PENDING,
        )
    except Exception:
        return None


def _carry_gap_orders(verify_data: dict[str, Any] | None, iteration: int) -> dict[str, Any] | None:
    """Gap order carrier to append to action_history, or None (gap-feed carrier).

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


def _recent_collection_surfaces(turns: Turns, limit: int = 10) -> list[str]:
    """Kind-qualified surfaces collected in recent turns (pure).

    File reads contribute ``file:<path>`` (from action parameters
    ``file_path``); symbol resolutions contribute ``symbol:<name>``
    (from ``symbol_name``). Kind-qualified so a collected file does not
    damp a symbol pivot on the same token and vice versa. Bounded to
    the trailing window; never raises. Takes the typed ``Turns`` view
    (F2-D18), never a raw history.
    """
    try:
        entries = list(turns or [])[-max(1, int(limit or 10)) :]
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


_RULES_BY_NAME: dict[str, Any] = {rule.name: rule for rule in RULES}


def _fired_forces(reason_data: Any) -> frozenset[Force]:
    """The forces whose flag is set on the reason data (pure, never raises)."""
    try:
        if not isinstance(reason_data, dict):
            return frozenset()
        return frozenset(force for force in Force if reason_data.get(force.value))
    except Exception:
        return frozenset()


def _stands_down(
    rule_name: str,
    reason_data: Any,
    *,
    is_defer: bool,
    absence_forced: bool,
    d6_fired: bool,
) -> bool:
    """Whether ``rule_name`` must not fire this turn (pure, fail-closed).

    True on a defer, absence-disconfirmation or D6 turn (turn state), or when any
    force in the rule's ``stands_down_on`` already fired. An unknown rule name or
    a fault stands down: no forcing is the safe outcome, the proposal passes
    through.
    """
    try:
        if is_defer or absence_forced or d6_fired:
            return True
        rule = _RULES_BY_NAME[rule_name]
        return bool(_fired_forces(reason_data) & rule.stands_down_on)
    except Exception:
        return True


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
    #: Carrier entry of the dispatched G3 requery order,
    #: for the workflow's consumed stamp. None when no order fired.
    consumed_order_ref: dict | None = None
    #: Carrier retirements this cascade decided (W2 D3; supersedes the W1 outputs
    #: outputs removed by W1-A). Each is {"event": "requery_order_retired",
    #: "reason": ConsumeReason value, "detail": short code,
    #: "claim_ref": str, "turn_issued": int, "age": int | None}.
    #: The workflow appends them to loop telemetry; nothing else reads them.
    order_events: list = field(default_factory=list)


@dataclass(frozen=True)
class CascadeContext:
    """Per-turn inputs one trigger may read (F8-D15, pure data)."""

    state: PostReasonCascadeState
    reason: dict[str, Any]  # current dispatch data; earlier rules may have rewritten it
    counts: Mapping[str, int]
    history: list[Any]  # full history: carrier readers index into it and stamp in place
    turns: Turns  # positional readers use this view, never the raw history (F2-D18)
    pivot_v2: bool  # lv7 hardened pivot
    policy: WorkOrderPolicy  # carrier semantics for this pin (W2-D2)

    def count(self, key: str) -> int:
        try:
            return int(self.counts.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0


@dataclass(frozen=True)
class Fire:
    """What one rule did: an override, effects only (``next_action is None``), or both."""

    next_action: str | None
    action_parameters: dict[str, Any] | None = None
    counter: str | None = None  # key in COUNTER_KEYS to bump
    extra: Mapping[str, Any] = field(default_factory=dict)  # payload keys stamped on the reason data
    consume: dict[str, Any] | None = None  # history entry stamped consumed at commit
    consumed_ref: dict[str, Any] | None = None  # carrier the workflow stamps after dispatch
    retire_reason: str = ""  # ConsumeReason value when consume retires without dispatch
    retire_detail: str = ""  # short machine code for the retirement event
    event: dict[str, Any] | None = None  # standalone order_events entry (e.g. gap_feed_stale)


def _entry_at(history: list[Any], index: Any) -> dict[str, Any] | None:
    try:
        entry = history[index]
    except (TypeError, IndexError, KeyError):
        return None
    return entry if isinstance(entry, dict) else None


def _t_pivot(ctx: CascadeContext) -> Fire | None:
    targets = _recent_collection_surfaces(ctx.turns)
    turns_left = int(ctx.state.max_iterations or 0) - int(ctx.state.iteration or 0)
    decision = _pivot_decision(
        ctx.state.prev_focus,
        ctx.reason,
        turns_left,
        targets,
        preserve_proposal=ctx.pivot_v2,
        forced_count=ctx.count("pivot"),
        hardened=ctx.policy.hardened,
    )
    if decision.reason is PivotReason.CAPPED:
        return Fire(None, counter="pivot_capped")
    if decision.reason is PivotReason.SCOPED_SEARCH:
        return Fire(None, counter="pivot_scoped_skipped")
    if decision.override is None:
        return None
    out = decision.override
    return Fire(out["next_action"], out["action_parameters"], counter="pivot" if ctx.pivot_v2 else None)


def _t_walk_stack(ctx: CascadeContext) -> Fire | None:
    params = _should_force_walk_stack(ctx.state.knowledge_seed, ctx.reason, ctx.history, ctx.count("walk_stack"))
    return Fire("WALK_STACK", params, counter="walk_stack") if params else None


def _t_requery(ctx: CascadeContext) -> Fire | None:
    out = _should_force_order_requery(
        ctx.history, recorded_attempt_summaries(ctx.history or []) or [], ctx.policy
    )
    if out is None:
        return None
    entry = _entry_at(ctx.history, out.get("order_index"))
    if out.get("mark_consumed"):  # retire the carrier, dispatch nothing
        if entry is None:
            return None
        return Fire(
            None,
            consume=entry,
            retire_reason=str(out.get("mark_consumed") or ConsumeReason.BUDGET_EXHAUSTED.value),
            retire_detail=str(out.get("detail") or ""),
        )
    return Fire(out["action_type"], out["action_parameters"], consumed_ref=entry)


def _t_floor(ctx: CascadeContext) -> Fire | None:
    s = ctx.state
    out = _explore_floor_target(
        ctx.reason, int(s.evidence_banked_total or 0), s.triage_route, s.description,
        s.knowledge_seed, ctx.count("floor"), history=ctx.history,
    )
    return Fire(out["next_action"], out["action_parameters"], counter="floor") if out else None


def _t_formulate(ctx: CascadeContext) -> Fire | None:
    s = ctx.state
    if not _should_force_formulate(
        int(s.iteration or 0), int(s.formulated_count or 0), ctx.reason.get("hypotheses_evaluated"),
        int(s.evidence_banked_total or 0), ctx.count("formulate"),
    ):
        return None
    return Fire("FORMULATE_HYPOTHESIS", {}, counter="formulate",
               extra={ForcePayload.FORMULATE_RULE.value: "formulate"})


def _t_synthesis(ctx: CascadeContext) -> Fire | None:
    s = ctx.state
    if not _should_force_synthesis(
        ctx.history, int(s.evidence_banked_total or 0), int(s.formulated_count or 0),
        ctx.count("synthesis"), ctx.reason,
    ):
        return None
    return Fire("FORMULATE_HYPOTHESIS", {}, counter="synthesis",
               extra={ForcePayload.FORMULATE_RULE.value: "synthesis"})


def _t_reread(ctx: CascadeContext) -> Fire | None:
    path = _reread_target(ctx.reason, ctx.history, ctx.count("reread"))
    return Fire("GET_CODE", {"file_path": path}, counter="reread") if path else None


def _t_callsite(ctx: CascadeContext) -> Fire | None:
    out = _callsite_target(
        ctx.reason, ctx.history, ctx.count("callsite"),
        tenant_id=ctx.state.tenant_id, investigation_id=str(ctx.state.investigation_id),
    )
    return Fire(out["next_action"], out["action_parameters"], counter="callsite") if out else None


def _t_log_search(ctx: CascadeContext) -> Fire | None:
    params = _should_force_log_search(str(ctx.state.description or ""), ctx.reason, ctx.history, ctx.count("log_search"))
    return Fire("SEARCH_LOGS", params, counter="log_search") if params else None


def _t_replan(ctx: CascadeContext) -> Fire | None:
    out = _should_force_replan(
        str(ctx.state.description or ""), ctx.reason, ctx.turns, ctx.count("replan"),
    )
    return Fire(out["next_action"], out["action_parameters"], counter="replan") if out else None


def _t_smallest_fetch(ctx: CascadeContext) -> Fire | None:
    orders: list[dict[str, Any]] = []
    carrier_turn: int | None = None
    for evt in reversed(ctx.history or []):  # newest feed carrier only (a tombstone supersedes older feeds)
        if isinstance(evt, dict) and evt.get("kind") == "work_order_feed":
            raw = evt.get("orders")
            orders = [o for o in raw if isinstance(o, dict)] if isinstance(raw, list) else []
            carrier_turn = _safe_int(evt.get("turn_issued", evt.get("iteration")))
            break
    if ctx.policy.hardened and carrier_turn is not None:
        try:
            feed_age = None if ctx.policy.iteration is None else int(ctx.policy.iteration) - int(carrier_turn)
        except Exception:
            feed_age = None
        if feed_age is not None and feed_age > GAP_ORDER_TTL_TURNS:
            orders = []  # stale feed: a keyless verify must not keep old gaps alive (W3-D20)
            return Fire(
                None,
                event={
                    "event": "gap_feed_stale",
                    "age": feed_age,
                    "turn_issued": carrier_turn,
                },
            )
    for _allow_retry in (False, True) if ctx.policy.hardened else (False,):
        for kind in SMALLEST_FETCH_ORDER:
            picked = _pick_gap_order(orders, kind=kind, history=ctx.history,
                                     forced_count=ctx.count(f"smallest_fetch_{kind.lower()}"),
                                     policy=ctx.policy, carrier_turn=carrier_turn,
                                     allow_retry=_allow_retry)
            if picked is None:
                continue
            gap_id = str(picked.gap_id or "")
            gap_reason = str(picked.gap_reason or "")
            context: dict[str, Any] = {"kind": kind, "gap_id": gap_id, "claim_ref": str(picked.claim_ref or "")}
            if gap_reason:
                context["reason"] = gap_reason
            return Fire(
                picked.action_type, dict(picked.action_parameters or {}),
                counter=f"smallest_fetch_{kind.lower()}",
                extra={
                    ForcePayload.SMALLEST_FETCH_KIND.value: kind,
                    ForcePayload.SMALLEST_FETCH_GAP_ID.value: gap_id,
                    "smallest_fetch_context": context,
                },
            )
    return None


Trigger = Callable[[CascadeContext], Fire | None]


TRIGGERS: dict[str, Trigger] = {
    "pivot": _t_pivot, "walk_stack": _t_walk_stack, "requery": _t_requery, "floor": _t_floor,
    "formulate": _t_formulate, "synthesis": _t_synthesis, "reread": _t_reread,
    "callsite": _t_callsite, "log_search": _t_log_search, "replan": _t_replan,
    "smallest_fetch": _t_smallest_fetch,
}


def check_triggers(rules: tuple[Any, ...] | list[Any], triggers: Mapping[str, Any]) -> list[str]:
    """Every rule has exactly one trigger and no trigger is orphaned (fail-closed at import)."""
    problems = []
    names = [r.name for r in rules]
    for name in names:
        if name not in triggers:
            problems.append(f"rule {name} has no trigger")
    for name in triggers:
        if name not in names:
            problems.append(f"trigger {name} has no rule")
    return problems


assert not check_triggers(RULES, TRIGGERS), check_triggers(RULES, TRIGGERS)


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
        # F2-D18: the typed turn view is built once; positional readers take it,
        # carrier readers take the full history. Single pin site.
        _turns = turn_view(_history, state.loop_version)
        # W2-D2: one policy value, built once next to the F2/F4 precedents.
        _wo_policy = WorkOrderPolicy(
            hardened=_work_orders_hardened(state.loop_version),
            iteration=_safe_int(state.iteration),
        )
        # F4 (lv7): bounded, non-destructive pivot anchored on the model's focus.
        _pivot_v2 = _pivot_hardened(state.loop_version)
        # The model's own focus this turn, read before any rule rewrites the
        # dispatch. A turn whose dispatch was forced before the cascade
        # (absence disconfirmation) carries no model focus.
        _proposal_focus = (
            focus_from_reason_data(_reason_data, hardened=_wo_policy.hardened)
            if _pivot_v2 and not _absence_forced
            else None
        )

        # F8: one definition of "this rule yields this turn". The D6 turn state
        # and its flag are one fact (production sets both together); each rule's
        # stand-down set lives in RULES. The loop below consults the table once
        # per rule, in order (F8-T13 pins this structurally).
        _d6_fired = _d6_fired or bool(_reason_data.get(Force.NOT_CHECKABLE.value))

        # F8-D15: one loop over RULES. Each rule's trigger adapter decides;
        # the loop applies. Stand-down is checked before the trigger runs, so a
        # yielding rule never evaluates its trigger. F7: everything is local
        # until the commit point below; a raising trigger faults the whole
        # cascade (proposal unrewritten, counters/stamps uncommitted, one log).
        reason: dict[str, Any] = dict(_reason_data)
        counts: dict[str, int] = dict(_counts)
        pending_consume: list[tuple[dict[str, Any], ConsumeReason, str]] = []
        consumed_ref: dict[str, Any] | None = None
        events: list[dict[str, Any]] = []
        fired_counter: dict[Any, str] = {}  # force -> counter bumped by the rule that wrote it
        fired: dict[str, Fire] = {}
        for rule in RULES:
            if _stands_down(
                rule.name,
                reason,
                is_defer=_is_defer,
                absence_forced=_absence_forced,
                d6_fired=_d6_fired,
            ):
                continue
            ctx = CascadeContext(state, reason, counts, _history, _turns, _pivot_v2, _wo_policy)
            fire = TRIGGERS[rule.name](ctx)
            if fire is None:
                continue
            if fire.event is not None:
                events.append(dict(fire.event))
            if fire.consume is not None:
                try:
                    _retire_reason = ConsumeReason(fire.retire_reason)
                except Exception:
                    _retire_reason = ConsumeReason.BUDGET_EXHAUSTED
                pending_consume.append((fire.consume, _retire_reason, fire.retire_detail))
                events.append(
                    _retirement_event(
                        fire.consume, _retire_reason,
                        {"detail": fire.retire_detail}, _wo_policy,
                    )
                )
                if _retire_reason is ConsumeReason.INVALID_ORDER:
                    try:
                        logger.warning(
                            "requery order invalid; carrier retired without dispatch: %s",
                            fire.retire_detail,
                            extra={
                                "event": "requery order invalid",
                                "error": fire.retire_detail,
                                "tenant_id": getattr(state, "tenant_id", ""),
                                "investigation_id": getattr(state, "investigation_id", ""),
                            },
                        )
                    except Exception:
                        pass
            if fire.next_action is None:
                # effects only: suppression counts and carrier retirements are
                # recorded without a dispatch, flag or cancel refund.
                if fire.counter:
                    counts[fire.counter] = ctx.count(fire.counter) + 1
                continue
            reason = {**reason, "next_action": fire.next_action,
                      "action_parameters": dict(fire.action_parameters or {}),
                      rule.force.value: True, **fire.extra}
            fired[rule.name] = fire
            if fire.counter:
                counts[fire.counter] = ctx.count(fire.counter) + 1
                fired_counter[rule.force] = fire.counter
            for cancelled in rule.cancels:  # F5: overwritten dispatch means the earlier rule did not happen
                reason[cancelled.value] = False
                refund = fired_counter.pop(cancelled, None)
                if refund:
                    counts[refund] = max(0, counts.get(refund, 0) - 1)
            if fire.consumed_ref is not None:
                consumed_ref = fire.consumed_ref
        _pivot_override = fired["pivot"].action_parameters if "pivot" in fired else None
        _reason_data = reason
        _counts = counts
        _pending_consume = pending_consume
        _consumed_order_ref = consumed_ref
        # P6 2b-i: record this turn's focus for next turn's pivot
        # comparison. lv6 and older read the focus post-cascade (the fetch the
        # loop actually dispatched), which let any forced fetch become the
        # anchor: the model's next unrelated proposal then looked like a pivot
        # and starved the carried orders (review F4). From lv7 the anchor is
        # the model's own proposal, read before any rule rewrote the dispatch;
        # damping still anchors on collected reality through the history
        # surfaces. Plain JSON scalars, replay-safe.
        _turn_focus = None
        try:
            _turn_focus = focus_from_reason_data(
                _reason_data if isinstance(_reason_data, dict) else {},
                hardened=_wo_policy.hardened,
            )
            if _pivot_v2:
                # F4 (lv7): anchor on the model's own proposal, not on a fetch
                # another rule forced, and leave the anchor alone when the
                # pivot was cancelled so it can refire. Forced and absence
                # turns carry no model focus (None) and leave it unchanged.
                _cancelled = _pivot_override is not None and not _reason_data.get(
                    Force.PIVOT_RECOLLECT.value
                )
                if _proposal_focus is not None and not _cancelled:
                    state.prev_focus = _proposal_focus.model_dump(mode="json")
            elif _turn_focus is not None:
                state.prev_focus = _turn_focus.model_dump(mode="json")
        except Exception:
            pass
        # W2-D3/D15: carrier retirements decided this turn, committed with
        # the stamps (F7) so a fault leaves carriers untouched.
        try:
            state.order_events = events
        except Exception:
            pass
        # F7 commit point: the whole cascade ran, so counters and consume
        # stamps become visible together with the chosen dispatch.
        for _stamped, _stamped_reason, _stamped_detail in _pending_consume:
            _stamped["consumed"] = True
            _stamped["consumed_reason"] = _stamped_reason.value
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
