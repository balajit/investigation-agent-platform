# src/investigation_agent_platform/application/worker/workflows.py
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from investigation_agent_platform.domain.common.background_job import (
        BackgroundJobStatus,
    )
    from investigation_agent_platform.domain.intake.batch import (
        MAX_ACTIVE_CHILDREN,
        child_workflow_id,
    )

with workflow.unsafe.imports_passed_through():
    from investigation_agent_platform.application.worker.activities import (
        CancelBatchChildrenInput,
        CaptureKnowledgeInput,
        CheckpointInput,
        CollectSnapshotsInput,
        CollectSnapshotsOutput,
        ConcludeInvestigationInput,
        CreateInvestigationInput,
        ExecuteActionInput,
        LoadBatchRecordsInput,
        MarkBatchRecordResultInput,
        MarkBatchRecordsInput,
        ProjectTopologyInput,
        PromoteFulfillmentEvidenceInput,
        PublishEventInput,
        ReasonInput,
        RecordInputRequirementInput,
        ResolveBatchStragglersInput,
        RetrieveEvidenceInput,
        RetrieveKnowledgeInput,
        RunClusteringInput,
        RunReindexInput,
        RunReportInput,
        SetInputRequirementStateInput,
        SummarizeBatchInput,
        SweepKnowledgeInput,
        TransitionInvestigationInput,
        UpdateBatchJobInput,
        UpdateClusteringJobInput,
        UpdateReindexJobInput,
        UpdateReportJobInput,
        VerifyRootCauseInput,
        cancel_batch_children_activity,
        capture_knowledge_activity,
        checkpoint_activity,
        collect_snapshots_activity,
        conclude_investigation_activity,
        create_investigation_activity,
        execute_action_activity,
        load_batch_records_activity,
        mark_batch_record_result_activity,
        mark_batch_records_activity,
        project_topology_activity,
        promote_fulfillment_evidence_activity,
        publish_event_activity,
        reason_activity,
        record_input_requirement_activity,
        resolve_batch_stragglers_activity,
        retrieve_evidence_activity,
        retrieve_knowledge_activity,
        run_clustering_activity,
        run_reindex_activity,
        run_report_activity,
        set_input_requirement_state_activity,
        summarize_batch_activity,
        sweep_knowledge_activity,
        transition_investigation_activity,
        update_batch_job_activity,
        update_clustering_job_activity,
        update_reindex_job_activity,
        update_report_job_activity,
        verify_root_cause_activity,
    )


@dataclass
class RunInvestigationInput:
    application_id: str
    session_id: str | None = None
    tenant_id: str = ""
    description: str | None = None
    correlation_id: str | None = None
    # The investigation was already created by the API before the workflow was
    # started (`wf-investigation-{investigation_id}`). This field is the
    # single source of truth for the investigation identity across the whole
    # run: API response, workflow ID, activity inputs, checkpoints, evidence,
    # and topology attribution must all refer to this ID. When absent (legacy
    # callers / tests), the workflow falls back to creating a new investigation.
    investigation_id: str | None = None
    # Iteration budget resolved by the API at dispatch from the effective
    # limits (global defaults plus the profile evidence_budget overlay).
    # None (legacy callers) means the local guard default.
    max_iterations: int | None = None


@dataclass
class RunInvestigationResult:
    investigation_id: str = ""
    status: str = ""
    error: str | None = None


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
        for key in ("forced_formulate", "forced_reread", "forced_log_search", "forced_walk_stack", "forced_replan"):
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
        action = str(
            reason_data.get("action", reason_data.get("next_action", "?")) or "?"
        )
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


def _classify_failure(
    description: str | None, intake_seed: dict | None
) -> str:
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
        "what", "where", "when", "which", "does", "doing", "have", "has",
        "had", "with", "from", "that", "this", "these", "those", "there",
        "their", "about", "show", "recent", "service", "services",
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


def _should_force_replan(
    description: str | None,
    reason_data: dict | None,
    action_history: list | None,
    forced_count: int,
) -> dict | None:
    """Alternative gathering action after repeated empties, or None.

    Fires when the two most recent ENRICHED turns (excluding the
    just-appended pre-execution entry) share one action with no evidence
    banked on either, and the current proposal repeats it. Returns
    {next_action, params} for the first alternative family, or None.
    Bounded (max 2 per run). Pure.
    """
    try:
        if int(forced_count or 0) >= 2:
            return None
        entries = [e for e in (action_history or []) if isinstance(e, dict)]
        # [-1] is the just-appended pre-execution entry (no outcome yet);
        # [-3:-1] are the last two enriched turns.
        if len(entries) < 3:
            return None
        prior_two = entries[-3:-1]
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
        alternative = next(
            (a for a in _REPLAN_ALTERNATIVES if a != actions[0]), None
        )
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


def _should_force_log_search(    description: str | None,
    reason_data: dict | None,
    action_history: list | None,
    forced_count: int,
) -> dict | None:
    """SEARCH_LOGS parameters to force, or None.

    Fires when the complaint uses error/log language, the model proposes a
    non-log gathering action, and no SEARCH_LOGS has banked evidence yet.
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
        if not any(pattern in lowered_desc for pattern in _LOG_FORCE_ERROR_PATTERNS):
            return None
        for entry in action_history or []:
            try:
                action = str((entry or {}).get("action", "")).upper()
                evidenced = int((entry or {}).get("evidence_added", 0) or 0)
            except (TypeError, ValueError):
                continue
            if action == "SEARCH_LOGS" and evidenced > 0:
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
        if not any(
            pattern in text
            for text in lowered
            for pattern in _REREAD_DEFINITION_PATTERNS
        ):
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
            cause = getattr(cause, "__cause__", None) or getattr(
                cause, "__context__", None
            )
            if not isinstance(cause, BaseException):
                return False
            seen += 1
        return False
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


@workflow.defn
class RunInvestigationWorkflow:
    def __init__(self) -> None:
        self._is_paused = False
        self._is_cancelled = False
        # Part 11.5: pending/fulfilled input requirement state. Plain JSON-able
        # dicts only, so a future Continue-As-New carries them without custom
        # serialization. The durable record of truth is the input_requirements
        # table; this memory is just the wait gate.
        self._pending_requirement: dict[str, Any] | None = None
        self._fulfilled_input: dict[str, Any] | None = None

    @workflow.signal
    async def pause(self) -> None:
        self._is_paused = True

    @workflow.signal
    async def resume(self) -> None:
        self._is_paused = False

    @workflow.signal
    async def cancel(self) -> None:
        self._is_cancelled = True

    @workflow.update
    async def fulfill_input(
        self, requirement_id: str, version: int, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Accept caller-supplied input for the pending requirement.

        Validation split: the API layer validates auth, tenant, JSON Schema,
        size, and the durable compare-and-set before invoking this Update; the
        validator below re-checks id/version against in-memory workflow state
        (validators cannot do I/O). Identical replays overwrite with identical
        content (no-op); conflicting content for the same version never
        reaches here because the API rejects it with 409 first.
        """
        self._fulfilled_input = {
            "requirement_id": requirement_id,
            "version": version,
            "data": data,
        }
        return {
            "accepted": True,
            "requirement_id": requirement_id,
            "version": version,
        }

    @fulfill_input.validator
    def validate_fulfill_input(
        self, requirement_id: str, version: int, data: dict[str, Any]
    ) -> None:
        pending = self._pending_requirement
        if pending is None:
            raise ApplicationError("no pending input requirement", non_retryable=True)
        if requirement_id != pending.get("requirement_id") or version != pending.get(
            "requirement_version"
        ):
            raise ApplicationError("stale or unknown input requirement", non_retryable=True)

    @workflow.run
    async def run(self, input_data: RunInvestigationInput) -> RunInvestigationResult:
        """Entry point: reconcile-then-report wrapper around the main loop.

        An unhandled exception anywhere in the loop previously failed the
        Temporal execution while the investigation aggregate stayed stranded
        in a non-terminal state (no FAILED marking, no audit record, control
        verbs 502ing against a closed execution). Now the aggregate is
        transitioned to FAILED first, and the execution reports the failure
        instead of dying silently. Cancellation always propagates.
        """
        try:
            return await self._run_inner(input_data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            workflow.logger.warning(
                "RunInvestigationWorkflow unhandled error; reconciling to FAILED: %s",
                type(exc).__name__,
            )
            # Surface the proximate activity cause (type + message only, no
            # payloads) so the failure record is diagnosable via the API.
            cause = getattr(exc, "cause", None)
            cause_type = type(cause).__name__ if cause is not None else type(exc).__name__
            cause_msg = str(getattr(cause, "message", "") or "")[:200]
            try:
                await workflow.execute_activity(
                    transition_investigation_activity,
                    TransitionInvestigationInput(
                        tenant_id=input_data.tenant_id,
                        investigation_id=input_data.investigation_id,
                        target_status="FAILED",
                        reason=f"Workflow error reconciled: {cause_type}",
                        failure_code="WORKFLOW_ERROR",
                        failure_reason=f"{cause_type}: {cause_msg}".strip(": "),
                    ),
                    start_to_close_timeout=timedelta(seconds=30),
                )
            except Exception as reconcile_exc:
                workflow.logger.warning(
                    "FAILED reconciliation transition failed: %s",
                    type(reconcile_exc).__name__,
                )
            return RunInvestigationResult(
                investigation_id=input_data.investigation_id,
                status="FAILED",
                error=f"workflow_error:{type(exc).__name__}",
            )

    async def _run_inner(
        self, input_data: RunInvestigationInput
    ) -> RunInvestigationResult:
        retry_policy = RetryPolicy(
            maximum_attempts=3,
            non_retryable_error_types=[
                "SecurityPolicyViolationException",
                "DomainValidationException",
                "InvalidHypothesisException",
                "PlatformConfigurationError",
            ],
        )

        # 1. Initialize investigation (state authority: persisted via InvestigationRepository).
        # F-IDENTITY: if the API already created the investigation, load it
        # instead of creating a second aggregate under a different ID.
        create_res = await workflow.execute_activity(
            create_investigation_activity,
            CreateInvestigationInput(
                application_id=input_data.application_id,
                session_id=input_data.session_id,
                tenant_id=input_data.tenant_id,
                description=input_data.description,
                investigation_id=input_data.investigation_id,
            ),
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry_policy,
        )

        inv_id = create_res.investigation_id
        tenant_id = input_data.tenant_id

        async def _transition(target: str, reason: str) -> None:
            await workflow.execute_activity(
                transition_investigation_activity,
                TransitionInvestigationInput(
                    tenant_id=tenant_id,
                    investigation_id=inv_id,
                    target_status=target,
                    reason=reason,
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )

        # Drive state machine: CREATED -> CONTEXTUALIZING -> INVESTIGATING
        await _transition("CONTEXTUALIZING", "Workflow started contextualizing")
        await _transition("INVESTIGATING", "Workflow entering investigation loop")

        async def _maybe_handle_input_required(
            result_data: dict[str, Any], resume_status: str
        ) -> str:
            """Handle an activity's ``input_required`` marker (Part 11.5).

            Returns "continue", "cancelled", or "expired". A pause does NOT
            block input fulfillment (pause halts work, not data delivery);
            cancellation and expiry do terminate the wait. On expiry the
            investigation transitions FAILED fail-closed: proceeding without
            required input would fabricate conclusions.
            """
            marker = result_data.get("input_required")
            if not marker or not isinstance(marker, dict):
                return "continue"
            record_res = await workflow.execute_activity(
                record_input_requirement_activity,
                RecordInputRequirementInput(
                    tenant_id=tenant_id,
                    investigation_id=inv_id,
                    reason=str(marker.get("reason", "Additional input required")),
                    json_schema=dict(marker.get("json_schema", {}) or {}),
                    classification=str(marker.get("classification", "INTERNAL")),
                    resume_status=resume_status,
                    promote_to_evidence=bool(marker.get("promote_to_evidence", False)),
                    expires_in_seconds=marker.get("expires_in_seconds"),
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
            if not record_res.success:
                workflow.logger.warning("input requirement recording failed: %s", record_res.error)
                return "continue"
            req_id = str(record_res.data.get("requirement_id", ""))
            req_version = int(record_res.data.get("requirement_version", 1))
            raw_timeout = record_res.data.get("wait_timeout_seconds")
            wait_timeout = float(raw_timeout) if isinstance(raw_timeout, (int, float)) else None
            await _transition("AWAITING_INPUT", f"Waiting for input: {req_id}")
            self._pending_requirement = {
                "requirement_id": req_id,
                "requirement_version": req_version,
            }
            self._fulfilled_input = None
            try:
                await workflow.wait_condition(
                    lambda: self._fulfilled_input is not None or self._is_cancelled,
                    timeout=wait_timeout,
                )
            except TimeoutError:
                await workflow.execute_activity(
                    set_input_requirement_state_activity,
                    SetInputRequirementStateInput(
                        tenant_id=tenant_id,
                        requirement_id=req_id,
                        expected_version=req_version,
                        state="EXPIRED",
                    ),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry_policy,
                )
                self._pending_requirement = None
                await _transition("FAILED", f"Input requirement {req_id} expired")
                return "expired"
            if self._is_cancelled and self._fulfilled_input is None:
                await workflow.execute_activity(
                    set_input_requirement_state_activity,
                    SetInputRequirementStateInput(
                        tenant_id=tenant_id,
                        requirement_id=req_id,
                        expected_version=req_version,
                        state="CANCELLED",
                    ),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry_policy,
                )
                self._pending_requirement = None
                return "cancelled"
            fulfilled: dict[str, Any] = self._fulfilled_input or {}
            await workflow.execute_activity(
                set_input_requirement_state_activity,
                SetInputRequirementStateInput(
                    tenant_id=tenant_id,
                    requirement_id=req_id,
                    expected_version=req_version,
                    state="FULFILLED",
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
            if record_res.data.get("promote_to_evidence"):
                await workflow.execute_activity(
                    promote_fulfillment_evidence_activity,
                    PromoteFulfillmentEvidenceInput(
                        tenant_id=tenant_id,
                        investigation_id=inv_id,
                        requirement_id=req_id,
                        data=dict(fulfilled.get("data", {}) or {}),
                        classification=str(marker.get("classification", "INTERNAL")),
                    ),
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=retry_policy,
                )
            resume = str(record_res.data.get("resume_status", resume_status))
            self._pending_requirement = None
            self._fulfilled_input = None
            await _transition(resume, f"Input {req_id} fulfilled; resuming")
            return "continue"

        # Budget guard: wall-clock + token/cost + max iterations.
        # Deterministic: use workflow.now() would be ideal but workflow.time is not available in all SDKs;
        # we rely on iteration cap + InvestigationBudget signal from reason/execute metadata.
        # The iteration cap resolves from RunInvestigationInput (API-resolved
        # from effective limits: global defaults plus profile overlay), with
        # the local guard as the backstop for legacy callers. Do not tune
        # investigation budgets here; change the domain defaults or the
        # profile instead.
        WORKFLOW_LOCAL_ITERATION_GUARD = 10
        # Legacy callers omit max_iterations: the guard above is the default,
        # so resolve against it explicitly instead of a second literal.
        requested = getattr(input_data, "max_iterations", None)
        max_iterations = _resolve_max_iterations(
            requested if requested is not None else WORKFLOW_LOCAL_ITERATION_GUARD
        )
        # Lazy import for type safety without failing workflow determinism checks.
        # Budget is evaluated via activity results; workflow keeps local iteration guard.
        iteration = 0
        verified = False
        last_error: str | None = None
        # Prior reason decisions (plain JSON scalars). Rebuilt deterministically
        # from activity results on replay; fed back into ReasonInput so the
        # model sees repetition and escalates capability instead of looping
        # the same empty action to the iteration cap.
        action_history: list[dict[str, Any]] = []
        # Cumulative LLM spend across iterations (plain scalars, replay-safe).
        # Fed into each checkpoint so token/cost totals survive worker
        # restarts; the reason activity enforces the per-investigation cap.
        llm_tokens_total = 0
        llm_cost_total = 0.0
        # G8 synthesis tracking (plain scalars, replay-safe): committed
        # formulations, banked evidence total, and forced-formulate count.
        formulated_count = 0
        evidence_banked_total = 0
        forced_formulate_count = 0
        forced_reread_count = 0
        forced_log_search_count = 0
        forced_walk_stack_count = 0
        forced_replan_count = 0
        forced_synthesis_count = 0
        overconfident_turns = 0
        # D3: triage route computed once at iteration 0 from the complaint
        # + intake seed; guides the reasoner, never decides alone.
        triage_route = "UNKNOWN"

        # 2. Main Investigation Loop — state authority is the workflow + checkpoint repo.
        while iteration < max_iterations and not self._is_cancelled:
            # Handle pause/resume/cancel signals.
            if self._is_paused:
                # TO_DO-013: a pause signal is a lifecycle event, not just an
                # in-memory flag — persist PAUSED through the transition
                # activity so investigation_transitions/timeline show it.
                # Resuming needs no explicit edge back: the next _transition
                # path-finds from PAUSED via the domain-derived graph (e.g.
                # PAUSED -> INVESTIGATING -> CORRELATING). Already-PAUSED is a
                # no-op inside the activity.
                await _transition("PAUSED", "Workflow pause signal handled")
            await workflow.wait_condition(lambda: not self._is_paused)
            if self._is_cancelled:
                break

            # Part 6 Slice 0: validity-gated knowledge retrieval before
            # reasoning. Degrades to unassisted reasoning on store trouble;
            # never fails the investigation. Result captured for reason
            # orientation (B-6: refdoc hits ride into the prompt as
            # orientation notes, never as evidence).
            knowledge_res = await workflow.execute_activity(
                retrieve_knowledge_activity,
                RetrieveKnowledgeInput(
                    tenant_id=tenant_id,
                    investigation_id=inv_id,
                    query=input_data.description,
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
            try:
                knowledge_orientation = list(
                    (knowledge_res.data or {}).get("reference_docs", []) or []
                )
            except Exception:
                knowledge_orientation = []
            # D2: intake seed for first-turn stack-walk forcing.
            try:
                _seed_raw = (knowledge_res.data or {}).get("intake_seed", None)
                knowledge_seed = dict(_seed_raw) if isinstance(_seed_raw, dict) else {}
            except Exception:
                knowledge_seed = {}
            # D3: triage once at iteration 0 (seed + complaint are run-fixed).
            if iteration == 0:
                try:
                    triage_route = _classify_failure(input_data.description, knowledge_seed)
                except Exception:
                    triage_route = "UNKNOWN"
                # E1: strategy skeleton recorded once; rides history (the
                # model sees it every turn) and telemetry.
                try:
                    investigation_plan = _investigation_plan(triage_route, knowledge_seed)
                except Exception:
                    investigation_plan = []

            # Reason — produces next action and readiness score.
            reason_res = await workflow.execute_activity(
                reason_activity,
                ReasonInput(
                    tenant_id=tenant_id,
                    investigation_id=inv_id,
                    iteration=iteration,
                    recent_actions=action_history[-10:],
                    knowledge_orientation=knowledge_orientation,
                ),
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=retry_policy,
            )
            # Budget guard: if reason signals budget exceeded, break.
            if reason_res.data.get("budget_exceeded"):
                last_error = "budget_exceeded"
                break
            # G8: synthesis forcing — thinking committed before more gathering.
            _reason_data = reason_res.data or {}
            # D2: stack-walk forcing first — explicit intake frames outrank
            # every other gathering rule.
            _walk_params = _should_force_walk_stack(
                knowledge_seed, _reason_data, action_history, forced_walk_stack_count
            )
            if _walk_params:
                forced_walk_stack_count += 1
                _reason_data = dict(_reason_data)
                _reason_data["next_action"] = "WALK_STACK"
                _reason_data["action_parameters"] = _walk_params
                _reason_data["forced_walk_stack"] = True
            if _should_force_formulate(
                iteration,
                formulated_count,
                _reason_data.get("hypotheses_evaluated"),
                evidence_banked_total,
                forced_formulate_count,
            ) and not _reason_data.get("forced_walk_stack"):
                forced_formulate_count += 1
                _reason_data = dict(_reason_data)
                _reason_data["next_action"] = "FORMULATE_HYPOTHESIS"
                _reason_data["forced_formulate"] = True
            # T2-4: synthesis forcing — rich multi-action corpus with
            # nothing committed gets FORMULATE (same override as G8).
            if not _reason_data.get("forced_walk_stack") and not _reason_data.get(
                "forced_formulate"
            ):
                if _should_force_synthesis(
                    action_history,
                    evidence_banked_total,
                    formulated_count,
                    forced_synthesis_count,
                    _reason_data,
                ):
                    forced_synthesis_count += 1
                    _reason_data = dict(_reason_data)
                    _reason_data["next_action"] = "FORMULATE_HYPOTHESIS"
                    _reason_data["forced_formulate"] = True
            # P0-2: re-read forcing — definition claim heading to VERIFY
            # without a range-read gets the read first (G3 floor would
            # INCONCLUSIVE the verify). Skipped when G8/D2 already forced.
            if not _reason_data.get("forced_formulate") and not _reason_data.get(
                "forced_walk_stack"
            ):
                _reread_path = _reread_target(
                    _reason_data, action_history, forced_reread_count
                )
                if _reread_path:
                    forced_reread_count += 1
                    _reason_data = dict(_reason_data)
                    _reason_data["next_action"] = "GET_CODE"
                    _reason_data["action_parameters"] = {"file_path": _reread_path}
                    _reason_data["forced_reread"] = True
            # B-4: log-first forcing — service-error complaints gather logs
            # before code/state. Skipped when G8/G3/D2 already forced.
            if (
                not _reason_data.get("forced_formulate")
                and not _reason_data.get("forced_reread")
                and not _reason_data.get("forced_walk_stack")
            ):
                _log_params = _should_force_log_search(
                    input_data.description,
                    _reason_data,
                    action_history,
                    forced_log_search_count,
                )
                if _log_params:
                    forced_log_search_count += 1
                    _reason_data = dict(_reason_data)
                    _reason_data["next_action"] = "SEARCH_LOGS"
                    _reason_data["action_parameters"] = _log_params
                    _reason_data["forced_log_search"] = True
            # E3: replan trigger last — two enriched empties on one surface
            # plus same proposal redirects to the alternative family.
            # Skipped when any earlier rule already forced.
            if (
                not _reason_data.get("forced_formulate")
                and not _reason_data.get("forced_reread")
                and not _reason_data.get("forced_log_search")
                and not _reason_data.get("forced_walk_stack")
            ):
                _replan = _should_force_replan(
                    input_data.description,
                    _reason_data,
                    action_history,
                    forced_replan_count,
                )
                if _replan:
                    forced_replan_count += 1
                    _reason_data = dict(_reason_data)
                    _reason_data["next_action"] = _replan["next_action"]
                    _reason_data["action_parameters"] = _replan["action_parameters"]
                    _reason_data["forced_replan"] = True
            _hyps = _reason_data.get("hypotheses_evaluated") or []
            _obss = _reason_data.get("observations") or []
            action_history.append(
                {
                    "iteration": iteration,
                    "action": str(_reason_data.get("next_action", "?")),
                    "readiness": _reason_data.get("conclusion_readiness"),
                    "hypotheses": len(_hyps) if isinstance(_hyps, list) else 0,
                    "observations": len(_obss) if isinstance(_obss, list) else 0,
                    "triage_route": triage_route,
                    # E1: strategy skeleton (iteration 0 only).
                    "plan": list(investigation_plan) if iteration == 0 else [],
                }
            )
            try:
                llm_tokens_total += int(_reason_data.get("llm_tokens", 0) or 0)
                llm_cost_total += float(_reason_data.get("llm_cost", 0.0) or 0.0)
            except (TypeError, ValueError):
                pass

            # Part 11.5: an activity may declare it needs caller-supplied
            # input before the run can proceed.
            input_state = await _maybe_handle_input_required(reason_res.data, "INVESTIGATING")
            if input_state == "cancelled":
                break
            if input_state == "expired":
                last_error = "input_expired"
                break

            # Execute proposed action (EvidenceGateway dispatch + sanitization + persistence).
            # G8: dispatch honors the synthesis-forced override in _reason_data.
            _dispatched_action = str(_reason_data.get("next_action", "QUERY_STATE"))
            try:
                execute_res = await workflow.execute_activity(
                    execute_action_activity,
                    ExecuteActionInput(
                        tenant_id=tenant_id,
                        investigation_id=inv_id,
                        action=_dispatched_action,
                        parameters=dict(_reason_data.get("action_parameters", {})),
                        hypotheses=list(_reason_data.get("hypotheses_evaluated", []) or []),
                        claims=list(_reason_data.get("claims", []) or []),
                    ),
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=retry_policy,
                )
            except ActivityError as exc:
                # Tranche B doctrine: an idempotency wedge (slow owner still
                # holding the key past the 3-attempt budget) degrades this
                # turn to EMPTY instead of reconciling the run FAILED. The
                # SDK surfaces activity failures as ActivityError wrapping
                # the ApplicationError, so walk the cause chain for the
                # wedge type; anything else re-raises.
                if not _caused_by_idempotency_wedge(exc):
                    raise
                execute_res = _degraded_empty_result(
                    _dispatched_action, "idempotency_owner_busy"
                )

            input_state = await _maybe_handle_input_required(execute_res.data, "INVESTIGATING")
            if input_state == "cancelled":
                break
            if input_state == "expired":
                last_error = "input_expired"
                break
            # Budget guard (execute side): the ledger routes here instead of
            # raising, so break to CONCLUDING with the gathered corpus.
            if execute_res.data.get("budget_exceeded"):
                last_error = "budget_exceeded"
                break
            # P0-2: close the loop on the pre-execution history entry — record
            # what was actually attempted and what it produced, so the next
            # reasoning turn can distinguish success/empty/degraded/failed.
            if action_history:
                _exec_data = execute_res.data or {}
                _outcome = _exec_data.get("action_outcome") or {}
                _params = dict(reason_res.data.get("action_parameters", {}) or {})
                action_history[-1].update(
                    {
                        "parameters": {
                            k: str(v)[:200] for k, v in _params.items()
                        },
                        "outcome_status": _outcome.get("status"),
                        "items_count": _outcome.get(
                            "item_count", _exec_data.get("items_count", 0)
                        ),
                        "evidence_added": len(_outcome.get("evidence_ids", []) or []),
                        "miss_reason": _outcome.get("miss_reason"),
                        "sources": _outcome.get("sources", []),
                        "forced_formulate": bool(_reason_data.get("forced_formulate", False)),
                        "forced_reread": bool(_reason_data.get("forced_reread", False)),
                        "forced_log_search": bool(_reason_data.get("forced_log_search", False)),
                        "forced_walk_stack": bool(_reason_data.get("forced_walk_stack", False)),
                        "forced_replan": bool(_reason_data.get("forced_replan", False)),
                    }
                )
                # G8: track synthesis state for the forcing rule.
                if _dispatched_action == "FORMULATE_HYPOTHESIS":
                    formulated_count += 1
                try:
                    evidence_banked_total += len(_outcome.get("evidence_ids", []) or [])
                except TypeError:
                    pass

            # Retrieve evidence (EvidenceGateway).
            retrieve_res = await workflow.execute_activity(
                retrieve_evidence_activity,
                RetrieveEvidenceInput(
                    tenant_id=tenant_id,
                    investigation_id=inv_id,
                    application_id=input_data.application_id,
                ),
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=retry_policy,
            )

            input_state = await _maybe_handle_input_required(retrieve_res.data, "INVESTIGATING")
            if input_state == "cancelled":
                break
            if input_state == "expired":
                last_error = "input_expired"
                break

            # Drive correlation/hypothesis/verification phases via state machine.
            # The reason decision rides along in each description so the
            # timeline read path shows what the reasoner attempted per
            # iteration (action, readiness, hypothesis/observation counts) —
            # previously only bare phase names were recorded and the
            # hypothesize step was a black box to API consumers.
            # C-1: merge the enriched history entry (outcome, forced flags)
            # over the raw decision so the timeline trace shows what was
            # attempted AND what it produced. Pre-execution keys survive
            # (disjoint names); absent enrichment renders the old shape.
            _decision_summary = _reason_summary(
                {**(reason_res.data or {}), **(action_history[-1] if action_history else {})}
            )
            await _transition("CORRELATING", f"Iteration {iteration} correlating ({_decision_summary})")
            await _transition("HYPOTHESIZING", f"Iteration {iteration} hypothesizing ({_decision_summary})")
            await _transition("VERIFYING", f"Iteration {iteration} verifying ({_decision_summary})")

            # Verify root cause (VerificationEngine).
            verify_res = await workflow.execute_activity(
                verify_root_cause_activity,
                VerifyRootCauseInput(
                    tenant_id=tenant_id,
                    investigation_id=inv_id,
                    triage_route=triage_route,
                ),
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=retry_policy,
            )

            input_state = await _maybe_handle_input_required(verify_res.data, "VERIFYING")
            if input_state == "cancelled":
                break
            if input_state == "expired":
                last_error = "input_expired"
                break

            # Checkpoint — persist InvestigationState snapshot via CheckpointRepository.
            await workflow.execute_activity(
                checkpoint_activity,
                CheckpointInput(
                    tenant_id=tenant_id,
                    investigation_id=inv_id,
                    step=iteration,
                    usage_totals={
                        "llm_tokens": llm_tokens_total,
                        "llm_cost": llm_cost_total,
                    },
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )

            if verify_res.data.get("verified"):
                verified = True
                break

            # Tier-3 critic instrumentation (zero-cost divergence proxy):
            # the model pushed to verify/conclude with high self-reported
            # readiness, but verification refused. Counts the turns a
            # critic would have overturned; a high rate across runs is the
            # build signal for Shape-1 critic. No extra LLM calls.
            try:
                _proposed = str((_reason_data or {}).get("next_action", "") or "").upper()
                _readiness = (reason_res.data or {}).get("conclusion_readiness", 0.0)
                _readiness_value = float(_readiness or 0.0)
            except (TypeError, ValueError):
                _proposed, _readiness_value = "", 0.0
            if _proposed in ("VERIFY_HYPOTHESIS", "CONCLUDE") or _readiness_value >= 0.8:
                overconfident_turns += 1

            # Also break on high conclusion readiness.
            readiness = reason_res.data.get("conclusion_readiness")
            if isinstance(readiness, (int, float)) and float(readiness) >= 0.95:
                break

            # Loop back: VERIFYING -> HYPOTHESIZING or INVESTIGATING for next iteration
            if iteration < max_iterations - 1 and not self._is_cancelled:
                await _transition(
                    "HYPOTHESIZING", f"Iteration {iteration} loop back to hypothesizing"
                )

            iteration += 1

        # 3. Conclude & Publish Events (EventPublisher on completion).
        await _transition("CONCLUDING", "Workflow entering concluding phase")
        if self._is_cancelled:
            final_status = "CANCELLED"
        elif verified:
            final_status = "COMPLETED"
        elif last_error == "budget_exceeded":
            final_status = "FAILED"
        else:
            # Not verified: reached max_iterations or readiness threshold without verification -> FAILED with limitations
            final_status = "FAILED"
            if last_error is None:
                last_error = "verification_failed_or_limit_reached"

        await workflow.execute_activity(
            conclude_investigation_activity,
            ConcludeInvestigationInput(
                tenant_id=tenant_id,
                investigation_id=inv_id,
                status=final_status,
                reason=last_error or "",
                # D5: triage route selects the runtime preset at verify +
                # conclude for failure-shaped runs.
                triage_route=triage_route,
                # C-2: loop counters ride into metadata["loop_telemetry"]
                # at conclude (plain scalars — replay-safe).
                loop_telemetry={
                    "llm_tokens_total": llm_tokens_total,
                    "llm_cost_total": llm_cost_total,
                    "formulated_count": formulated_count,
                    "evidence_banked_total": evidence_banked_total,
                    "forced_formulate_count": forced_formulate_count,
                    "forced_reread_count": forced_reread_count,
                    "forced_log_search_count": forced_log_search_count,
                    "forced_walk_stack_count": forced_walk_stack_count,
                    "forced_replan_count": forced_replan_count,
                    "forced_synthesis_count": forced_synthesis_count,
                    "overconfident_turns": overconfident_turns,
                    "iterations": iteration,
                    "triage_route": triage_route,
                    "plan": list(investigation_plan),
                },
            ),
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry_policy,
        )

        # Part 6 Slice 0: distill concluded state into knowledge envelopes.
        # Capture failures never fail the investigation.
        await workflow.execute_activity(
            capture_knowledge_activity,
            CaptureKnowledgeInput(tenant_id=tenant_id, investigation_id=inv_id),
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry_policy,
        )

        await workflow.execute_activity(
            publish_event_activity,
            PublishEventInput(
                tenant_id=tenant_id, investigation_id=inv_id, event="INVESTIGATION_FINISHED"
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )

        return RunInvestigationResult(
            investigation_id=inv_id, status=final_status, error=last_error
        )


@dataclass
class TopologySnapshotRetentionInput:
    """One scheduled retention pass over a set of repositories (ISSUE-4).

    Intended to be started on a Temporal ``Schedule`` (e.g. daily) — either
    one schedule per ``(tenant_id, repository_id)`` pair, or one schedule
    invoking this workflow with the full list an operator maintains. Each
    repository is collected via its own retried activity so one failing
    repository never blocks the rest of the pass.
    """

    repositories: list[tuple[str, str]]  # (tenant_id, repository_id) pairs


@dataclass
class TopologySnapshotRetentionResult:
    per_repository: dict[str, CollectSnapshotsOutput]


@dataclass
class KnowledgeArtifactJanitorInput:
    """Scheduled janitor pass over a set of tenants (ISSUE-9)."""

    tenants: list[str]
    reverify_conditional: bool = False


@dataclass
class KnowledgeArtifactJanitorResult:
    per_tenant: dict[str, int]


@workflow.defn
class KnowledgeArtifactJanitorWorkflow:
    """Scheduled sweep: TTL-elapsed -> EXPIRED via the retrieval transition
    path. Idempotent; never mutates history, only advances status."""

    @workflow.run
    async def run(
        self, input_data: KnowledgeArtifactJanitorInput
    ) -> KnowledgeArtifactJanitorResult:
        retry_policy = RetryPolicy(maximum_attempts=3)
        output = await workflow.execute_activity(
            sweep_knowledge_activity,
            SweepKnowledgeInput(
                tenants=list(input_data.tenants),
                reverify_conditional=input_data.reverify_conditional,
            ),
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=retry_policy,
        )
        data = output.data if hasattr(output, "data") else {}
        return KnowledgeArtifactJanitorResult(per_tenant=dict(data.get("per_tenant", {})))


@workflow.defn
class TopologySnapshotRetentionWorkflow:
    """Scheduled janitor: classifies and collects unpinned, out-of-window
    topology snapshots per repository (ISSUE-4). Never touches a snapshot
    referenced by an open investigation's own recorded evidence, and never
    deletes a ``TopologySnapshot`` audit node — only the AST subgraph
    beneath it.
    """

    @workflow.run
    async def run(
        self, input_data: TopologySnapshotRetentionInput
    ) -> TopologySnapshotRetentionResult:
        retry_policy = RetryPolicy(maximum_attempts=3)
        results: dict[str, CollectSnapshotsOutput] = {}
        for tenant_id, repository_id in input_data.repositories:
            output = await workflow.execute_activity(
                collect_snapshots_activity,
                CollectSnapshotsInput(tenant_id=tenant_id, repository_id=repository_id),
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=retry_policy,
            )
            results[f"{tenant_id}/{repository_id}"] = output
        return TopologySnapshotRetentionResult(per_repository=results)


@dataclass
class FindingClusteringInput:
    tenant_id: str
    job_id: str = ""
    limit: int = 500
    batch_size: int = 25


@dataclass
class FindingClusteringResult:
    tenant_id: str = ""
    job_id: str = ""
    examined: int = 0
    assigned: int = 0
    unassigned: int = 0
    new_clusters: int = 0
    taxonomy_revision: int = 0
    error: str | None = None


@workflow.defn
class FindingClusteringWorkflow:
    """Scheduled/on-demand incremental finding clustering (Part 11.7).

    Single-activity run keeps history tiny regardless of corpus size;
    resumability comes from assignment persistence (re-invocation skips
    assigned findings). Progress is tracked on the linked BackgroundJob row.
    """

    @workflow.run
    async def run(self, input_data: FindingClusteringInput) -> FindingClusteringResult:
        retry_policy = RetryPolicy(
            maximum_attempts=3,
            non_retryable_error_types=[
                "SecurityPolicyViolationException",
                "DomainValidationException",
                "PlatformConfigurationError",
            ],
        )
        if input_data.job_id:
            await workflow.execute_activity(
                update_clustering_job_activity,
                UpdateClusteringJobInput(
                    tenant_id=input_data.tenant_id,
                    job_id=input_data.job_id,
                    status=BackgroundJobStatus.RUNNING.value,
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
        res = await workflow.execute_activity(
            run_clustering_activity,
            RunClusteringInput(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                limit=input_data.limit,
                batch_size=input_data.batch_size,
            ),
            start_to_close_timeout=timedelta(seconds=1800),
            retry_policy=retry_policy,
        )
        data = res.data
        if not res.success:
            await workflow.execute_activity(
                update_clustering_job_activity,
                UpdateClusteringJobInput(
                    tenant_id=input_data.tenant_id,
                    job_id=input_data.job_id,
                    status=BackgroundJobStatus.FAILED.value,
                    error=data.get("error"),
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
            return FindingClusteringResult(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                error=data.get("error"),
            )
        await workflow.execute_activity(
            update_clustering_job_activity,
            UpdateClusteringJobInput(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                status=BackgroundJobStatus.DONE.value,
                stage={
                    "name": "clustered",
                    "message": (
                        f"examined={data.get('examined', 0)} "
                        f"assigned={data.get('assigned', 0)} "
                        f"unassigned={data.get('unassigned', 0)}"
                    ),
                },
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )
        return FindingClusteringResult(
            tenant_id=input_data.tenant_id,
            job_id=input_data.job_id,
            examined=int(data.get("examined", 0)),
            assigned=int(data.get("assigned", 0)),
            unassigned=int(data.get("unassigned", 0)),
            new_clusters=int(data.get("new_clusters", 0)),
            taxonomy_revision=int(data.get("taxonomy_revision", 0)),
        )


@dataclass
class AggregateReportInput:
    tenant_id: str
    job_id: str = ""
    legal_hold: bool = False


@dataclass
class AggregateReportResult:
    tenant_id: str = ""
    job_id: str = ""
    taxonomy_revision: int = 0
    assignment_count: int = 0
    artifact_keys: list[str] = field(default_factory=list)
    error: str | None = None


@workflow.defn
class AggregateReportWorkflow:
    """On-demand aggregate reporting over one taxonomy generation (Part 11.8).

    Bounded by construction: taxonomy ≤25 clusters, members ≤200 per
    cluster, one render activity. History stays tiny, so no Continue-As-New
    is required (inputs cannot approach history/event thresholds).
    Progress is tracked on the linked BackgroundJob row.
    """

    @workflow.run
    async def run(self, input_data: AggregateReportInput) -> AggregateReportResult:
        retry_policy = RetryPolicy(
            maximum_attempts=3,
            non_retryable_error_types=[
                "SecurityPolicyViolationException",
                "DomainValidationException",
                "PlatformConfigurationError",
            ],
        )
        if input_data.job_id:
            await workflow.execute_activity(
                update_report_job_activity,
                UpdateReportJobInput(
                    tenant_id=input_data.tenant_id,
                    job_id=input_data.job_id,
                    status=BackgroundJobStatus.RUNNING.value,
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
        res = await workflow.execute_activity(
            run_report_activity,
            RunReportInput(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                legal_hold=input_data.legal_hold,
            ),
            start_to_close_timeout=timedelta(seconds=1800),
            retry_policy=retry_policy,
        )
        data = res.data
        if not res.success:
            await workflow.execute_activity(
                update_report_job_activity,
                UpdateReportJobInput(
                    tenant_id=input_data.tenant_id,
                    job_id=input_data.job_id,
                    status=BackgroundJobStatus.FAILED.value,
                    error=data.get("error"),
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
            return AggregateReportResult(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                error=data.get("error"),
            )
        await workflow.execute_activity(
            update_report_job_activity,
            UpdateReportJobInput(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                status=BackgroundJobStatus.DONE.value,
                stage={
                    "name": "reported",
                    "message": (
                        f"revision={data.get('taxonomy_revision', 0)} "
                        f"assignments={data.get('assignment_count', 0)}"
                    ),
                },
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )
        return AggregateReportResult(
            tenant_id=input_data.tenant_id,
            job_id=input_data.job_id,
            taxonomy_revision=int(data.get("taxonomy_revision", 0)),
            assignment_count=int(data.get("assignment_count", 0)),
            artifact_keys=list(data.get("artifact_keys", [])),
        )


@dataclass
class ReferenceDocsIndexInput:
    tenant_id: str
    job_id: str = ""
    source_id: str = ""
    application_id: str = ""


@dataclass
class ReferenceDocsIndexResult:
    tenant_id: str = ""
    job_id: str = ""
    source_id: str = ""
    generation: int = 0
    files_seen: int = 0
    chunks_written: int = 0
    tombstoned: int = 0
    error: str | None = None


@workflow.defn
class ReferenceDocsIndexWorkflow:
    """Source indexing over bounded runs (Part 11.6).

    One bounded activity per run (file/chunk caps enforced in the service);
    larger sources partition into multiple jobs rather than growing one
    history. Resumability across failures comes from content-hash compare
    (re-invocation rewrites nothing unchanged). Progress is tracked on the
    linked BackgroundJob row.
    """

    @workflow.run
    async def run(self, input_data: ReferenceDocsIndexInput) -> ReferenceDocsIndexResult:
        retry_policy = RetryPolicy(
            maximum_attempts=3,
            non_retryable_error_types=[
                "SecurityPolicyViolationException",
                "DomainValidationException",
                "PlatformConfigurationError",
            ],
        )
        if input_data.job_id:
            await workflow.execute_activity(
                update_reindex_job_activity,
                UpdateReindexJobInput(
                    tenant_id=input_data.tenant_id,
                    job_id=input_data.job_id,
                    status=BackgroundJobStatus.RUNNING.value,
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
        res = await workflow.execute_activity(
            run_reindex_activity,
            RunReindexInput(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                source_id=input_data.source_id,
                application_id=input_data.application_id,
            ),
            start_to_close_timeout=timedelta(seconds=1800),
            retry_policy=retry_policy,
        )
        data = res.data
        if not res.success:
            await workflow.execute_activity(
                update_reindex_job_activity,
                UpdateReindexJobInput(
                    tenant_id=input_data.tenant_id,
                    job_id=input_data.job_id,
                    status=BackgroundJobStatus.FAILED.value,
                    error=data.get("error"),
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
            return ReferenceDocsIndexResult(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                source_id=input_data.source_id,
                error=data.get("error"),
            )
        await workflow.execute_activity(
            update_reindex_job_activity,
            UpdateReindexJobInput(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                status=BackgroundJobStatus.DONE.value,
                stage={
                    "name": "indexed",
                    "message": (
                        f"generation={data.get('generation', 0)} "
                        f"files={data.get('files_seen', 0)} "
                        f"chunks={data.get('chunks_written', 0)}"
                    ),
                },
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )
        return ReferenceDocsIndexResult(
            tenant_id=input_data.tenant_id,
            job_id=input_data.job_id,
            source_id=input_data.source_id,
            generation=int(data.get("generation", 0)),
            files_seen=int(data.get("files_seen", 0)),
            chunks_written=int(data.get("chunks_written", 0)),
            tombstoned=int(data.get("tombstoned", 0)),
        )


@dataclass
class BulkIntakeInput:
    tenant_id: str
    job_id: str = ""
    start_index: int = 0
    attempt: int = 0
    patience_seconds: int = 3600


@dataclass
class BulkIntakeResult:
    tenant_id: str = ""
    job_id: str = ""
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    awaiting_input: int = 0
    canceled: int = 0
    continued_as_new: bool = False
    error: str | None = None


def should_continue_as_new(
    start_index: int, dispatched_this_run: int, remaining: int, active_count: int
) -> bool:
    """Pure CAN predicate (Part 11.9): roll only with zero active children.

    Fires when this run already dispatched past CAN_RECORD_THRESHOLD and
    work remains. Results and idempotency live in record rows, so the new
    execution resumes purely from the dispatch cursor.
    """
    from investigation_agent_platform.domain.intake.batch import CAN_RECORD_THRESHOLD

    return (
        active_count == 0
        and remaining > 0
        and start_index + dispatched_this_run >= CAN_RECORD_THRESHOLD
    )


@workflow.defn
class BulkIntakeWorkflow:
    """Bounded bulk fan-out over pre-created investigations (Part 11.9).

    Children are `RunInvestigationWorkflow` executions with deterministic
    opaque ids (`wf-batch-{job}-{index}` + `-r{attempt}` on retry). At most
    MAX_ACTIVE_CHILDREN run concurrently — a window slot is held from child
    start through completion, never start-only. Parent close policy is
    ABANDON (awaiting-input children survive parent completion); parent
    cancellation fans out explicitly through cancel_batch_children_activity
    (auditable per child). Continue-As-New fires only with zero active
    children, carrying the dispatch cursor + attempt; results and
    idempotency live in the record rows, never in workflow memory. Children
    never retry at workflow level (maximum_attempts=1): a re-run would
    replay CREATED→CONTEXTUALIZING against an already-advanced
    investigation and violate the lifecycle. Transient faults are absorbed
    by activity-level retries; permanent child failure lands on the record
    row for API-level partial retry with attempt-suffixed child ids.
    """

    @workflow.run
    async def run(self, input_data: BulkIntakeInput) -> BulkIntakeResult:
        import asyncio as _asyncio

        retry_policy = RetryPolicy(
            maximum_attempts=3,
            non_retryable_error_types=[
                "SecurityPolicyViolationException",
                "DomainValidationException",
                "PlatformConfigurationError",
            ],
        )
        if input_data.job_id:
            await workflow.execute_activity(
                update_batch_job_activity,
                UpdateBatchJobInput(
                    tenant_id=input_data.tenant_id,
                    job_id=input_data.job_id,
                    status=BackgroundJobStatus.RUNNING.value,
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
        loaded = await workflow.execute_activity(
            load_batch_records_activity,
            LoadBatchRecordsInput(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                start_index=input_data.start_index,
            ),
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=retry_policy,
        )
        if not loaded.success:
            return await self._fail(input_data, retry_policy, str(loaded.error or "load failed"))
        pending: list[dict[str, Any]] = list(loaded.data.get("records", []))
        total: int = int(loaded.data.get("total", 0))
        if not pending:
            return await self._finish(input_data, retry_policy, total)

        active: dict[int, Any] = {}
        queue = pending
        dispatched = 0
        try:
            try:
                async with _asyncio.timeout(input_data.patience_seconds):
                    while queue or active:
                        while queue and len(active) < MAX_ACTIVE_CHILDREN:
                            record = queue.pop(0)
                            index = int(record["record_index"])
                            child_id = child_workflow_id(
                                input_data.job_id,
                                index,
                                input_data.attempt,
                            )
                            # String workflow type: the SDK resolves the
                            # definition at runtime; the class object does
                            # not satisfy the static child-workflow signature.
                            task = _asyncio.create_task(
                                self._run_one_child(input_data, record, index, child_id)
                            )
                            await workflow.execute_activity(
                                mark_batch_records_activity,
                                MarkBatchRecordsInput(
                                    tenant_id=input_data.tenant_id,
                                    job_id=input_data.job_id,
                                    record_indices=[index],
                                    status="RUNNING",
                                    child_workflow_ids={str(index): child_id},
                                ),
                                start_to_close_timeout=timedelta(seconds=60),
                                retry_policy=retry_policy,
                            )
                            active[index] = task
                            dispatched += 1
                        if not active:
                            break
                        done, _pending_tasks = await _asyncio.wait(
                            set(active.values()), return_when=_asyncio.FIRST_COMPLETED
                        )
                        for task in done:
                            index, child_status, child_error = await task
                            active.pop(index, None)
                            await workflow.execute_activity(
                                mark_batch_record_result_activity,
                                MarkBatchRecordResultInput(
                                    tenant_id=input_data.tenant_id,
                                    job_id=input_data.job_id,
                                    record_index=index,
                                    status=child_status,
                                    error=child_error,
                                ),
                                start_to_close_timeout=timedelta(seconds=60),
                                retry_policy=retry_policy,
                            )
                            if should_continue_as_new(
                                input_data.start_index, dispatched, len(queue), len(active)
                            ):
                                return await self._continue_as_new(
                                    input_data, retry_policy, queue[0]["record_index"]
                                )
            except TimeoutError:
                await workflow.execute_activity(
                    resolve_batch_stragglers_activity,
                    ResolveBatchStragglersInput(
                        tenant_id=input_data.tenant_id,
                        job_id=input_data.job_id,
                        record_indices=sorted(active),
                    ),
                    start_to_close_timeout=timedelta(seconds=300),
                    retry_policy=retry_policy,
                )
        except _asyncio.CancelledError:
            # Cancellation propagates explicitly to live children (ABANDON
            # close policy keeps them alive otherwise); best-effort here,
            # the API already marked the job CANCEL_REQUESTED.
            try:
                await workflow.execute_activity(
                    cancel_batch_children_activity,
                    CancelBatchChildrenInput(
                        tenant_id=input_data.tenant_id, job_id=input_data.job_id
                    ),
                    start_to_close_timeout=timedelta(seconds=300),
                    retry_policy=retry_policy,
                )
                await workflow.execute_activity(
                    update_batch_job_activity,
                    UpdateBatchJobInput(
                        tenant_id=input_data.tenant_id,
                        job_id=input_data.job_id,
                        status=BackgroundJobStatus.CANCELED.value,
                    ),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry_policy,
                )
            except Exception:
                pass
            raise
        return await self._finish(input_data, retry_policy, total)

    async def _run_one_child(
        self, input_data: BulkIntakeInput, record: dict[str, Any], index: int, child_id: str
    ) -> tuple[int, str, str | None]:
        """Execute one child investigation; never raises (Part 11.9).

        Returns (record_index, terminal_status, error). Awaiting the child
        handle directly keeps the window slot held start→completion.
        """
        try:
            result = await workflow.execute_child_workflow(
                "RunInvestigationWorkflow",
                RunInvestigationInput(
                    application_id=str(record.get("application_id", "")),
                    tenant_id=input_data.tenant_id,
                    description=None,
                    investigation_id=record.get("investigation_id"),
                ),
                id=child_id,
                parent_close_policy=ParentClosePolicy.ABANDON,
                execution_timeout=timedelta(seconds=7200),
                run_timeout=timedelta(seconds=7200),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            terminal = str(getattr(result, "status", "") or "").upper()
            if terminal in ("COMPLETED", "DONE", "SUCCESS"):
                return (index, "DONE", None)
            if terminal in ("FAILED", "ERROR"):
                return (index, "FAILED", getattr(result, "error", None))
            if getattr(result, "error", None):
                return (index, "FAILED", str(getattr(result, "error", None))[:500])
            return (index, "DONE", None)
        except Exception as exc:
            return (index, "FAILED", str(exc)[:500])

    async def _finish(
        self, input_data: BulkIntakeInput, retry_policy: Any, total: int
    ) -> BulkIntakeResult:
        summary = await workflow.execute_activity(
            summarize_batch_activity,
            SummarizeBatchInput(tenant_id=input_data.tenant_id, job_id=input_data.job_id),
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry_policy,
        )
        data = summary.data if summary.success else {}
        await workflow.execute_activity(
            update_batch_job_activity,
            UpdateBatchJobInput(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                status=BackgroundJobStatus.DONE.value,
                stage={
                    "name": "intake-complete",
                    "message": (
                        f"total={data.get('total', total)} "
                        f"succeeded={data.get('succeeded', 0)} "
                        f"failed={data.get('failed', 0)} "
                        f"awaiting={data.get('awaiting_input', 0)}"
                    ),
                },
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )
        return BulkIntakeResult(
            tenant_id=input_data.tenant_id,
            job_id=input_data.job_id,
            total=int(data.get("total", total)),
            succeeded=int(data.get("succeeded", 0)),
            failed=int(data.get("failed", 0)),
            awaiting_input=int(data.get("awaiting_input", 0)),
            canceled=int(data.get("canceled", 0)),
        )

    async def _fail(
        self, input_data: BulkIntakeInput, retry_policy: Any, error: str
    ) -> BulkIntakeResult:
        await workflow.execute_activity(
            update_batch_job_activity,
            UpdateBatchJobInput(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                status=BackgroundJobStatus.FAILED.value,
                error=error,
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )
        return BulkIntakeResult(
            tenant_id=input_data.tenant_id, job_id=input_data.job_id, error=error
        )

    async def _continue_as_new(
        self, input_data: BulkIntakeInput, retry_policy: Any, next_index: int
    ) -> BulkIntakeResult:
        # Zero active children is guaranteed by the caller (post-settle,
        # `active` empty). Results + idempotency live in record rows.
        await workflow.execute_activity(
            update_batch_job_activity,
            UpdateBatchJobInput(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                status="RUNNING",
                stage={"name": "continue-as-new", "message": f"cursor={next_index}"},
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )
        workflow.continue_as_new(
            BulkIntakeInput(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                start_index=next_index,
                attempt=input_data.attempt,
                patience_seconds=input_data.patience_seconds,
            )
        )
        raise RuntimeError("unreachable: continue_as_new never returns")


@dataclass
class TopologyProjectionInput:
    """One scheduled topology projection pass (cognee pilot).

    Intended for a Temporal Schedule per (tenant, repository) or on webhook
    push. `revision` empty resolves from the checkout HEAD in the activity;
    `changed_files` is recorded for future incremental work.
    """

    tenant_id: str
    repository_id: str
    revision: str = ""
    checkout: str = ""
    application_id: str = ""
    changed_files: list[str] = field(default_factory=list)


@dataclass
class TopologyProjectionResult:
    status: str
    dataset: str = ""
    points: int = 0
    edges: int = 0
    revision: str = ""


@workflow.defn
class TopologyProjectionWorkflow:
    """Scheduled projection: local checkout → deterministic topology points.

    Idempotent per (tenant, repository, revision): re-projecting the same
    revision rewrites identical point IDs (no duplicates). Skips (never
    fails the schedule) when the pilot is disabled or the checkout is
    absent — genuine projection failures retry per repository policy.
    """

    @workflow.run
    async def run(
        self, input_data: TopologyProjectionInput
    ) -> TopologyProjectionResult:
        retry_policy = RetryPolicy(maximum_attempts=3)
        output = await workflow.execute_activity(
            project_topology_activity,
            ProjectTopologyInput(
                tenant_id=input_data.tenant_id,
                repository_id=input_data.repository_id,
                revision=input_data.revision,
                checkout=input_data.checkout,
                application_id=input_data.application_id,
                changed_files=list(input_data.changed_files),
            ),
            start_to_close_timeout=timedelta(seconds=600),
            retry_policy=retry_policy,
        )
        data = output.data if hasattr(output, "data") else {}
        return TopologyProjectionResult(
            status=str(data.get("status", "unknown")),
            dataset=str(data.get("dataset", "")),
            points=int(data.get("points", 0) or 0),
            edges=int(data.get("edges", 0) or 0),
            revision=str(data.get("revision", "")),
        )
