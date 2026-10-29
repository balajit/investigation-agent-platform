# src/investigation_agent_platform/application/worker/workflows/forcing.py
from __future__ import annotations

import hashlib
import re
from typing import Any

from temporalio import workflow

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


def _carry_ledger_directive(verify_data: dict | None, iteration: int) -> dict | None:
    """G3 directive marker to append to action_history, or None.

    Pure carry step (D3 lifecycle step 2): validates the verify result's
    ``ledger_requery`` dict minimally (non-empty query, known retryable
    action type) and returns the ONE new history entry. Never merges
    into an existing entry. Strictly additive — None for anything
    unexpected, so a malformed directive never breaks the loop here;
    full validation happens at convert time. Pure — workflow-safe.
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
        try:
            attempt_index = int(directive.get("attempt_index", 0))
        except (TypeError, ValueError):
            attempt_index = 0
        try:
            max_attempts = int(directive.get("max_attempts", WP_A3_DEFAULT_MAX_ATTEMPTS))
        except (TypeError, ValueError):
            max_attempts = WP_A3_DEFAULT_MAX_ATTEMPTS
        return {
            "kind": "ledger_requery_directive",
            "iteration": int(iteration),
            "claim_ref": str(directive.get("claim_ref") or ""),
            "query": query,
            "scope": _normalize_scope(directive.get("scope")),
            "action_type": action_type,
            "action_parameters": dict(directive.get("action_parameters") or {}),
            "attempt_index": attempt_index,
            "max_attempts": max(1, max_attempts),
            "turn_issued": int(iteration),
            "consumed": False,
        }
    except Exception:
        return None


def _should_force_ledger_requery(
    action_history: list | None,
    recorded_summaries: list | None,
) -> dict | None:
    """G3 ledger forcing decision (D3 lifecycle step 3: convert).

    Returns the typed override (``action_type``, ``action_parameters``,
    ``marker_index``) when an unconsumed directive marker fires, or
    ``{"mark_consumed": reason, "marker_index": index}`` when the newest
    unconsumed marker must be retired without dispatch (malformed
    directive or exhausted budget), or None when the turn proceeds
    unchanged. Reads only workflow history plus same-turn summaries —
    never the process-local ledger. Pure — workflow-safe.

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
        if entry.get("kind") != "ledger_requery_directive":
            continue
        if entry.get("consumed"):
            continue
        candidate_index = index
        break
    if candidate_index is None:
        return None
    marker = entries[candidate_index]
    try:
        consumed = directive_consumed(marker, entries, recorded)
    except Exception:
        return None
    if consumed:
        return {"mark_consumed": "budget-exhausted", "marker_index": candidate_index}
    query = str(marker.get("query") or "").strip()
    action_type = str(marker.get("action_type") or "").upper()
    parameters = marker.get("action_parameters")
    if (
        not query
        or action_type not in RETRYABLE_ACTIONS
        or not isinstance(parameters, dict)
        or not all(
            isinstance(k, str) and (v is None or isinstance(v, (str, int, float, bool)))
            for k, v in parameters.items()
        )
    ):
        return {"mark_consumed": "invalid-directive", "marker_index": candidate_index}
    return {
        "action_type": action_type,
        "action_parameters": dict(parameters),
        "marker_index": candidate_index,
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


def _source_value_target(
    reason_data: dict | None,
    action_history: list | None,
    forced_count: int,
) -> dict[str, Any] | None:
    """Request exact Java/Python source before accepting a field or method value."""
    try:
        if int(forced_count or 0) >= 2:
            return None
        data = reason_data or {}
        if str(data.get("next_action", "")).upper() != "VERIFY_HYPOTHESIS":
            return None
        for claim in data.get("claims", []) or []:
            if not isinstance(claim, dict):
                continue
            semantics = str(
                claim.get("value_semantics")
                or (claim.get("target") or {}).get("value_semantics", "")
            ).upper()
            fact_category = str(
                claim.get("fact_category") or (claim.get("target") or {}).get("fact_category", "")
            ).upper()
            method_return = (
                fact_category == "SOURCE_VALUE_FACT" and semantics == "GUARDED_RETURN_LITERAL"
            )
            field_value = str(
                claim.get("claim_kind", "")
            ).upper() == "DEFINITION" and semantics in {
                "EXPLICIT_INITIALIZER",
                "LANGUAGE_DEFAULT",
                "PARAMETER_DEFAULT_EXPRESSION",
                "FRAMEWORK_DEFAULT",
            }
            if not (method_return or field_value):
                continue
            target = claim.get("target") or {}
            if not isinstance(target, dict):
                continue
            symbol = str(target.get("symbol", "") or "").strip()
            file_path = str(target.get("file_path", "") or "").strip()
            if not symbol or not file_path or not file_path.lower().endswith((".java", ".py")):
                continue
            if any(
                isinstance(action, dict)
                and str(action.get("action", "")).upper() == "GET_CODE"
                and int(action.get("evidence_added", 0) or 0) > 0
                and str((action.get("parameters") or {}).get("file_path", "")).strip() == file_path
                and (
                    str((action.get("parameters") or {}).get("method_name", "")).strip()
                    == symbol.rsplit(".", 1)[-1]
                    if method_return
                    else str((action.get("parameters") or {}).get("field_name", "")).strip()
                    == symbol
                )
                for action in (action_history or [])
            ):
                continue
            params: dict[str, Any] = {"file_path": file_path}
            params["method_name" if method_return else "field_name"] = (
                symbol.rsplit(".", 1)[-1] if method_return else symbol
            )
            if method_return and target.get("guard_condition"):
                params["guard_condition"] = str(target["guard_condition"])
            for target_key, param_key in (
                ("declaring_type", "declaring_type"),
                ("revision", "revision"),
            ):
                if target.get(target_key):
                    params[param_key] = str(target[target_key])
            # Parse from the file head so the class declaration/braces and
            # field's declaration context are present. The provider caps the
            # actual returned bytes/lines and marks truncation; the analyzer
            # fails closed if that bounded window is incomplete.
            params["start_line"] = 1
            params["end_line"] = 2000
            return params
        # L5 fallback: a BEHAVIOR-typed claim with no fact_category heading
        # to VERIFY can never satisfy the runtime half in a code-only scope,
        # and the typed method/field path above never fires without a typed
        # claim (chicken-and-egg: no typed claim -> no method read -> no
        # resolution -> INCONCLUSIVE). When a banked GET_CODE range read for
        # a .java/.py file exists without any method/field variant read yet,
        # force one full-window re-read so the model sees the method bodies
        # and can retype as SOURCE_VALUE_FACT (prompt-directed) on the next
        # turn. Bounded by the same forced_count cap; never guesses a method
        # name, never touches the BEHAVIOR bar or profile doctrine.
        for claim in data.get("claims", []) or []:
            if not isinstance(claim, dict):
                continue
            if str(claim.get("claim_kind", "") or "").upper() != "BEHAVIOR":
                continue
            _fallback_category = str(
                claim.get("fact_category") or (claim.get("target") or {}).get("fact_category", "")
            ).strip()
            if _fallback_category:
                continue
            _banked_files: list[str] = []
            for action in action_history or []:
                if not isinstance(action, dict):
                    continue
                if str(action.get("action", "") or "").upper() != "GET_CODE":
                    continue
                if int(action.get("evidence_added", 0) or 0) <= 0:
                    continue
                _params = action.get("parameters") or {}
                if not isinstance(_params, dict):
                    continue
                _file = str(_params.get("file_path", "") or "").strip()
                if not _file or not _file.lower().endswith((".java", ".py")):
                    continue
                if str(_params.get("method_name", "") or "").strip():
                    continue
                if str(_params.get("field_name", "") or "").strip():
                    continue
                if _file not in _banked_files:
                    _banked_files.append(_file)
            if not _banked_files:
                continue
            return {
                "file_path": _banked_files[-1],
                "start_line": 1,
                "end_line": 2000,
            }
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
) -> dict[str, Any] | None:
    """Discriminating gather action before thin terminal proposals, or None.

    Fires when the model proposes a committal action (formulate/verify/
    conclude) with fewer than 3 banked rows on a code-shaped incident.
    L1#2 died after a single path-only read: the absence branch it took
    can never verify, so the loop must read first instead of letting the
    run commit to a one-row theory. Bounded (max 2 per run); pure.
    """
    try:
        data_action = str((reason_data or {}).get("next_action", "") or "").upper()
        if data_action not in _FLOOR_TERMINAL_ACTIONS:
            return None
        if int(forced_count or 0) >= 2:
            return None
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
        if not code_shaped:
            return None
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
                    if "/" in token and len(token) >= 3:
                        return {
                            "next_action": "GET_CODE",
                            "action_parameters": {"file_path": token},
                        }
            except Exception:
                continue
        for key in ("failing_symbol", "caller_symbol"):
            try:
                symbol = str(seed.get(key, "") or "").strip()
                if symbol:
                    return {
                        "next_action": "FIND_SYMBOL",
                        "action_parameters": {"symbol_name": symbol},
                    }
            except Exception:
                continue
        keywords = _description_keywords(description)
        if not keywords:
            return None
        return {
            "next_action": "SEARCH_CODE",
            "action_parameters": {"query": " ".join(keywords[:3])},
        }
    except Exception:
        return None


def _should_force_replan(
    description: str | None,
    reason_data: dict | None,
    action_history: list | None,
    forced_count: int,
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
        entries = [e for e in (action_history or []) if isinstance(e, dict)]
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


def _smallest_fetch_target(
    action_history: list[dict[str, Any]] | None,
    forced_count: int,
    gaps: list[dict[str, Any]] | None,
    *,
    kind: str,
    tenant_id: str = "",
    investigation_id: str = "",
) -> dict[str, Any] | None:
    """One smallest-fetch override for a single claim kind, or None (pure).

    Fires when the D2 gap feed names an open gap of this kind. ``gaps``
    is the latest claim_gap_feed history entry's list, passed in by the
    cascade — never read from reason_data. Bounded (max 2 per run per
    kind). Content-free by design: gap identity + history coverage
    decide. Never raises.
    """
    try:
        if int(forced_count or 0) >= _FORCED_SMALLEST_FETCH_DEFAULT_CAP:
            return None
        spec = KIND_FETCH.get(str(kind or ""))
        if not spec:
            return None
        feed = [g for g in (gaps or []) if isinstance(g, dict)]
        gap = None
        try:
            for entry in feed:
                try:
                    if str(entry.get("kind") or "") != str(kind):
                        continue
                    if not str(entry.get("gap_id") or ""):
                        continue
                    # Covered gaps fall through to the next open gap of
                    # the same kind — a stale feed never suppresses its
                    # siblings.
                    if _smallest_fetch_already_covered(
                        action_history, str(entry.get("gap_id") or "")
                    ):
                        continue
                    gap = entry
                    break
                except Exception:
                    # Skip the malformed sibling, never abort the kind:
                    # one bad entry must not suppress its open siblings.
                    continue
        except Exception:
            return None
        if gap is None:
            return None
        try:
            builder = (spec or {}).get("builder")
            params = builder(gap) if callable(builder) else None
        except Exception:
            return None
        if not params:
            return None
        try:
            action = _smallest_fetch_action_for(str(kind), dict(params))
            params = {k: v for k, v in dict(params).items() if not str(k).startswith("_")}
        except Exception:
            return None
        if not action:
            return None
        return {
            "next_action": action,
            "action_parameters": dict(params),
            "gap_id": str(gap.get("gap_id") or ""),
            "claim_ref": str(gap.get("claim_ref") or ""),
            "fetch_kind": str(kind),
        }
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


def _carry_claim_gap_feed(
    verify_data: dict[str, Any] | None, iteration: int
) -> dict[str, Any] | None:
    """Claim-gap feed marker to append to action_history, or None (pure).

    Pure carry: validates verify result data["claim_gaps"] is a list. A
    non-empty list must contain dicts with a non-empty gap_id and a known
    KIND_FETCH kind. An empty list returns a valid tombstone entry, which
    supersedes older feeds. Only malformed or missing input returns None,
    so a malformed feed never breaks the loop. Feed entries are deduped
    by gap_id (first wins); drops are counted, not silent.
    Workflow-safe (no wall-clock, no randomness). Never raises.
    """
    try:
        data = verify_data if isinstance(verify_data, dict) else {}
        raw = data.get("claim_gaps")
        if not isinstance(raw, list):
            return None
        gaps: list[dict[str, Any]] = []
        seen: set[str] = set()
        dropped_unknown_kind = 0
        dropped_missing_gap_id = 0
        dropped_duplicate = 0
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
                gaps.append(dict(entry))
            except Exception:
                dropped_missing_gap_id += 1
                continue
        if raw and not gaps:
            # Non-empty input but nothing valid: malformed, not a tombstone.
            return None
        return {
            "kind": "claim_gap_feed",
            "iteration": int(iteration),
            "turn_issued": int(iteration),
            "gaps": gaps,
            "dropped": {
                "unknown_kind": dropped_unknown_kind,
                "missing_gap_id": dropped_missing_gap_id,
                "duplicate_gap_id": dropped_duplicate,
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


def _should_force_conclude_not_checkable(
    reason_data: dict[str, Any] | None,
) -> bool:
    """True when honest abstention is the only possible outcome (pure).

    Every evaluated statement has ≥1 matching claim dict and every
    claim dict is NOT_CHECKABLE. Empty sets, unmatched statements, and
    any checkable claim return False. Never raises.
    """
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
