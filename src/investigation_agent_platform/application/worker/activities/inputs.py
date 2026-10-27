# src/investigation_agent_platform/application/worker/activities/inputs.py
"""Temporal activity thin adapters — each delegates to the application/domain layer via AppContext."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

from .common import COMP_ACTVTY, OUTCOME_SKIPPED, story_log


@dataclass
class CreateInvestigationInput:
    application_id: str
    session_id: str | None = None
    tenant_id: str = ""
    description: str | None = None
    # Existing investigation ID (F-IDENTITY): when set, the activity MUST load
    # this investigation rather than creating a new aggregate. This preserves
    # one investigation_id end-to-end across API, workflow, checkpoints,
    # evidence, and topology attribution.
    investigation_id: str | None = None


@dataclass
class CreateInvestigationOutput:
    investigation_id: str
    status: str


@dataclass
class GenericActivityResult:
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


# Typed activity inputs — tenant_id + investigation_id consistently required.


@dataclass
class ReasonInput:
    tenant_id: str
    investigation_id: str
    iteration: int = 0
    # Prior iterations' reason decisions (plain JSON scalars, most recent
    # last), accumulated by the workflow. Lets the model see repetition
    # (e.g. SEARCH_LOGS ×N with empty evidence) and escalate capability
    # instead of looping the same empty action to the iteration cap.
    recent_actions: list = field(default_factory=list)
    # B-6: orientation notes (refdoc hits, knowledge facts) as plain JSON
    # scalars. Orientation only — never evidence; the validator drops
    # citations that do not resolve to banked evidence rows.
    knowledge_orientation: list = field(default_factory=list)


def _caller_edge_candidates_from_evidence(evidence: list[Any]) -> list[dict[str, Any]]:
    """Project provider-located call-graph edges as discovery candidates."""
    candidates: list[dict[str, Any]] = []
    for item in evidence or []:
        try:
            evidence_type = str(
                getattr(getattr(item, "evidence_type", None), "value", "")
                or getattr(item, "evidence_type", "")
            ).upper()
            if evidence_type != "CALL_GRAPH":
                continue
            attrs = getattr(item, "attributes", None) or {}
            if not isinstance(attrs, dict):
                continue
            caller = str(attrs.get("caller_symbol", "") or "").strip()
            callee = str(attrs.get("callee_symbol", "") or "").strip()
            location = getattr(getattr(item, "provenance", None), "source_location", None)
            file_path = str(getattr(location, "file_path", "") or "").strip()
            line = getattr(location, "line_start", None)
            if not caller or not callee or not file_path or not isinstance(line, int):
                continue
            candidates.append(
                {
                    "evidence_id": str(getattr(item, "evidence_id", "") or ""),
                    "caller": caller,
                    "callee": callee,
                    "file_path": file_path,
                    "line_start": line,
                    "line_end": getattr(location, "line_end", None) or line,
                    "revision": str(getattr(location, "revision", "") or ""),
                    "discovery_complete": False,
                }
            )
        except Exception:
            continue
    return candidates[:20]


@dataclass
class ExecuteActionInput:
    tenant_id: str
    investigation_id: str
    action: str = "QUERY_STATE"
    parameters: dict[str, Any] = field(default_factory=dict)
    # Reason-proposed hypotheses (decision.hypotheses_evaluated) carried by
    # the workflow so FORMULATE_HYPOTHESIS can persist them. Empty for all
    # other actions.
    hypotheses: list[str] = field(default_factory=list)
    # Phase 2.1: typed claims (decision.claims) with per-claim evidence
    # binding. Empty for legacy callers; FORMULATE falls back to the
    # batch-wide supporting_evidence_ids parameter for statements without one.
    claims: list[dict[str, Any]] = field(default_factory=list)
    # Provider-discovered caller edges carried from reason activity. These are
    # candidate obligations, not proof; formulation must union them with model
    # claims before building required_verification.
    caller_edge_candidates: list[dict[str, Any]] = field(default_factory=list)
    # G3: workflow loop iteration driving this action. None (legacy
    # callers) means the attempt id is left untagged rather than guessed.
    iteration: int | None = None
    # Phase 4 (claim binding): workflow-only smallest-fetch identity
    # ({"kind", "gap_id", "claim_ref"}) carried beside params, never
    # inside them. None (legacy callers and non-forced actions) means
    # no fetch_reason stamp. The execution bank helper stamps banked
    # rows from this context; it is never forwarded to providers,
    # never enters extract_search_query, and never touches ledger keys.
    smallest_fetch_context: dict[str, Any] | None = None


@dataclass
class RetrieveEvidenceInput:
    tenant_id: str
    investigation_id: str
    application_id: str | None = None


@dataclass
class VerifyRootCauseInput:
    tenant_id: str
    investigation_id: str
    # D5: triage route selects the runtime verification preset for
    # failure-shaped runs; empty (legacy callers) keeps the default.
    triage_route: str = ""
    # G3: prior-turn history entries (most recent last) carried by the
    # workflow so verify can re-derive timeline-backed recorded attempts.
    # Empty for legacy callers; verify then keeps legacy diagnostics.
    recent_actions: list = field(default_factory=list)
    # G3: workflow loop iteration driving this verify turn. None (legacy
    # callers) leaves the evaluation marker's turn untagged rather than
    # guessed; timestamps still order evaluations.
    iteration: int | None = None


@dataclass
class CheckpointInput:
    tenant_id: str
    investigation_id: str
    step: int = 0
    state_snapshot: dict[str, Any] | None = None
    # Cumulative LLM spend resolved by the workflow (same from-start basis
    # as the checkpoint counters, but fresher). Overrides carried-forward
    # values when present; never sums with them (that would double-count).
    usage_totals: dict[str, Any] | None = None


@dataclass
class ConcludeInvestigationInput:
    tenant_id: str
    investigation_id: str
    status: str
    # Workflow terminal reason (last_error: budget_exceeded, input_expired,
    # verification_failed_or_limit_reached, ...). Recorded as a stable
    # failure code on FAILED aggregates so the API can report it.
    reason: str = ""
    # C-2: workflow loop counters (plain JSON scalars). Stamped onto
    # metadata["loop_telemetry"] with derived repo counts so budgets and
    # hypothesis lifecycle are API-visible without worker-log access.
    # None (legacy callers) means telemetry unstamped.
    loop_telemetry: dict[str, Any] | None = None
    # D5: triage route selects the runtime verification preset for
    # failure-shaped runs; empty (legacy callers) keeps the default.
    triage_route: str = ""
    # D3 T3 receipt pairing: the workflow's current receipt index and
    # loop iteration so conclude-gate envelopes continue the run's
    # ordered receipt chain (defaults keep legacy callers working).
    definition_receipt_start: int = 0
    conclude_iteration: int = 0


def _stamp_loop_telemetry(inv: Any, telemetry: dict[str, Any] | None) -> Any:
    """Attach loop telemetry to investigation metadata (no I/O).

    Mirrors _stamp_failure: stamped BEFORE the status transition so it
    rides the normal save path. No-op when telemetry is None (legacy).
    """
    if not telemetry:
        return inv
    stamped_metadata = dict(getattr(inv, "metadata", None) or {})
    stamped = {str(key): telemetry[key] for key in sorted(telemetry) if isinstance(key, str)}
    # Variance triage (#29/#50): pin the effective sampler config alongside
    # the loop counters so future rerun comparisons can rule out config
    # drift. Activity context (not workflow code), so env reads are safe.
    try:
        import os as _os

        _provider = str(_os.environ.get("IAP_LLM_PROVIDER", "") or "").strip()
        _model = ""
        if _provider:
            _model = str(_os.environ.get(f"IAP_{_provider.upper()}_MODEL", "") or "").strip()
        stamped.setdefault("llm_provider", _provider)
        stamped.setdefault("llm_model", _model)
        stamped.setdefault(
            "llm_temperature", str(_os.environ.get("IAP_LLM_TEMPERATURE", "0.0") or "0.0")
        )
    except Exception:
        pass
    stamped_metadata["loop_telemetry"] = stamped
    return inv.model_copy(update={"metadata": stamped_metadata})


def _failure_code_for(reason: str, concluded_from_completed_request: bool) -> str:
    """Normalize a workflow conclude reason to a stable terminal failure code.

    Called only when the resolved target status is FAILED. A COMPLETED
    request that lands FAILED was denied by the conclusion gate
    (ConclusionGate blockers), which takes precedence as the proximate cause.
    """
    if concluded_from_completed_request:
        return "CONCLUSION_DENIED"
    normalized = (reason or "").strip().lower()
    if normalized == "budget_exceeded":
        return "BUDGET_EXCEEDED"
    if normalized == "input_expired":
        return "INPUT_EXPIRED"
    if normalized == "verification_failed_or_limit_reached":
        return "UNVERIFIED"
    return "UNKNOWN"


def _stamp_failure(inv: Any, code: str, reason: str) -> Any:
    """Attach a terminal failure record to investigation metadata (no I/O).

    The stamp must happen BEFORE the status transition so it rides the
    normal save path (model_copy preserves metadata; SQLAlchemy persists it
    via metadata_json). Shared by the conclude path and the workflow-error
    reconcile path so every FAILED aggregate reports its cause via the API.
    """
    stamped_metadata = dict(getattr(inv, "metadata", None) or {})
    stamped_metadata["failure"] = {
        "code": code,
        "reason": (reason or "")[:500],
        "failed_at": datetime.now(UTC).isoformat(),
    }
    return inv.model_copy(update={"metadata": stamped_metadata})


def _select_verification_policy(triage_route: Any) -> Any:
    """Runtime preset for failure-shaped runs, default otherwise (D5).

    Any non-UNKNOWN route means the run triaged a failure (transient,
    config, data, bug) rather than a plain question — select the runtime
    preset so band tuning has one home. Pure.
    """
    try:
        from investigation_agent_platform.application.investigation.verification import (
            RootCauseVerificationPolicy,
        )

        route = str(triage_route or "").upper()
        if route and route != "UNKNOWN":
            return RootCauseVerificationPolicy.runtime()
        return RootCauseVerificationPolicy()
    except Exception:
        from investigation_agent_platform.application.investigation.verification import (
            RootCauseVerificationPolicy,
        )

        return RootCauseVerificationPolicy()


def _derive_loop_telemetry(
    passed: Any,
    hypothesis_counts: dict[str, int],
    evidence_banked: int,
) -> dict[str, Any] | None:
    """Merge workflow counters with derived repo counts (pure).

    C-2: passed holds llm_tokens_total/llm_cost_total/formulated and forced
    counters from the workflow; hypothesis_counts carries formulated/bound/
    verified from the repo; evidence_banked is the repo row count. None in
    (legacy caller) means None out — telemetry unstamped.
    """
    try:
        if not isinstance(passed, dict) or not passed:
            return None
        telemetry: dict[str, Any] = {}
        for key in (
            "llm_tokens_total",
            "llm_cost_total",
            "formulated_count",
            "evidence_banked_total",
            "forced_formulate_count",
            "forced_reread_count",
            "forced_log_search_count",
            "forced_walk_stack_count",
            "forced_replan_count",
            "forced_synthesis_count",
            "overconfident_turns",
            "iterations",
        ):
            try:
                value = passed.get(key)
            except Exception:
                continue
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, (int, float)):
                telemetry[key] = value
        try:
            route = passed.get("triage_route", None)
            if isinstance(route, str) and route:
                telemetry["triage_route"] = route[:32]
        except Exception:
            pass
        if isinstance(hypothesis_counts, dict):
            for key in ("hypotheses_formulated", "hypotheses_bound", "hypotheses_verified"):
                try:
                    value = int(hypothesis_counts.get(key, 0) or 0)
                except (TypeError, ValueError):
                    continue
                telemetry[key] = max(0, value)
        # Phase 1 (claim binding): bounded per-run binding counts from the
        # workflow-accumulated verify-turn totals (non-negative ints only;
        # absent on legacy runs). Version is a closed label, never free text.
        for key in (
            "claim_binding_kept",
            "claim_binding_evaporated",
            "claim_binding_unmapped",
            "claim_binding_repaired",
            "claim_binding_repair_shadow",
        ):
            try:
                value = int(passed.get(key, 0) or 0)
            except (TypeError, ValueError):
                continue
            telemetry[key] = max(0, value)
        try:
            _cbv = str(passed.get("claim_binding_version", "") or "")
            if _cbv in ("cbv1", "cbv2", "cbv3"):
                telemetry["claim_binding_version"] = _cbv
        except Exception:
            pass
        try:
            telemetry["evidence_banked_repo"] = max(0, int(evidence_banked or 0))
        except (TypeError, ValueError):
            pass
        # G6-measure: pass the shadow trace/summary through (capped,
        # shape-checked; anything malformed is dropped, never fatal).
        # Existing keys above are untouched.
        try:
            summary = passed.get("shadow_summary")
            if isinstance(summary, dict):
                telemetry["shadow_summary"] = {
                    k: max(0, int(summary.get(k, 0) or 0))
                    for k in ("turns", "matched", "capture_errors", "eligible", "diverged")
                }
        except Exception:
            pass
        try:
            trace = passed.get("shadow_trace")
            if isinstance(trace, list):
                kept: list[dict[str, Any]] = []
                for turn in trace[:256]:
                    if not isinstance(turn, dict):
                        continue
                    entry = {
                        k: turn.get(k)
                        for k in (
                            "iteration",
                            "recorded",
                            "selected",
                            "match",
                            "cause",
                            "shadow_terminal",
                            "capture_error",
                            "evidence_banked_total",
                            "prev_outcome",
                            "params",
                            "forced",
                            "owed",
                        )
                    }
                    if isinstance(entry.get("iteration"), bool):
                        continue
                    # Optional fields are shape-checked, never turn-dropping:
                    # coerce malformed values to empty so a bad optional
                    # cannot silently remove a comparable turn (capture
                    # health tracks missing turns, never silent drops).
                    if not isinstance(entry.get("params", {}), dict):
                        entry["params"] = {}
                    if "forced" in entry and not isinstance(entry["forced"], list):
                        entry["forced"] = []
                    if "owed" in entry and not isinstance(entry["owed"], dict):
                        entry["owed"] = {}
                    if not isinstance(entry.get("params"), (dict, type(None))):
                        continue
                    kept.append(entry)
                telemetry["shadow_trace"] = kept
        except Exception:
            pass
        # D3 Phase 0: shadow-definition summary + bounded projected trace
        # (counts and allowlisted scalar keys only — same shape-checking,
        # caps, and fail-open isolation as the G6 block above; the
        # projection helper is the single implementation, tested in
        # test_d3_shadow_bundle/test_d3_shadow_runtime. G6 keys above are
        # untouched.)
        try:
            from investigation_agent_platform.domain.proof.definition_shadow import (
                project_trace_for_telemetry as _project_d3_trace,
            )

            dsummary = passed.get("shadow_definition_summary")
            dtrace = passed.get("definition_shadow_trace")
            # N21 additive-key rule: no envelopes, no shadow keys. An empty
            # trace means nothing was observed (flag off, or no verify turn
            # produced an observation) — stamping zeroed blocks would fake
            # a shadow presence the control arm must prove absent.
            # G6 shadow_trace/shadow_summary predate D3 and are untouched.
            _has_shadow_turns = isinstance(dtrace, list) and len(dtrace) > 0
            if isinstance(dsummary, dict) and _has_shadow_turns:
                telemetry["shadow_definition_summary"] = {
                    k: max(0, int(dsummary.get(k, 0) or 0))
                    for k in (
                        "turns",
                        "admissible",
                        "abstained",
                        "unavailable",
                        "capture_errors",
                        "mirror_notes",
                        "dropped_count",
                        "duplicate_count",
                        "telemetry_omissions",
                    )
                }
                # §1.6 frozen-inventory breakdowns (additive dicts, key/value
                # capped and shape-checked like the counts above).
                for dict_key in ("by_input_set", "by_reason"):
                    try:
                        raw_breakdown = dsummary.get(dict_key)
                        if isinstance(raw_breakdown, dict):
                            clean: dict[str, int] = {}
                            for bk, bv in list(raw_breakdown.items())[:64]:
                                if isinstance(bk, str) and bk and not isinstance(bv, bool):
                                    try:
                                        clean[bk[:128]] = max(0, int(bv or 0))
                                    except (TypeError, ValueError):
                                        continue
                            telemetry["shadow_definition_summary"][dict_key] = clean
                    except Exception:
                        pass
            dtrace = passed.get("definition_shadow_trace")
            if isinstance(dtrace, list) and len(dtrace) > 0:
                projected, health = _project_d3_trace(dtrace)
                telemetry["definition_shadow_trace"] = projected
                if isinstance(telemetry.get("shadow_definition_summary"), dict):
                    telemetry["shadow_definition_summary"]["dropped_count"] = health[
                        "dropped_count"
                    ]
        except Exception:
            pass
        return telemetry or None
    except Exception:
        return None


async def _count_hypothesis_lifecycle(
    ctx: Any, tenant_id: str, investigation_id: Any
) -> dict[str, int]:
    """Formulated/bound/verified hypothesis counts (fail-open to zeros)."""
    counts = {"hypotheses_formulated": 0, "hypotheses_bound": 0, "hypotheses_verified": 0}
    try:
        repo: Any = getattr(ctx, "hypothesis_repo", None)
        if repo is None:
            return counts
        hyps = await repo.find_by_investigation_id(tenant_id, investigation_id)
        counts["hypotheses_formulated"] = len(hyps or [])
        for hyp in hyps or []:
            try:
                supporting = getattr(hyp, "supporting_evidence_ids", None) or []
                refuting = getattr(hyp, "refuting_evidence_ids", None) or []
                if len(list(supporting)) + len(list(refuting)) > 0:
                    counts["hypotheses_bound"] += 1
                status = str(
                    getattr(getattr(hyp, "status", ""), "value", getattr(hyp, "status", ""))
                ).upper()
                if status == "VERIFIED":
                    counts["hypotheses_verified"] += 1
            except Exception:
                continue
    except Exception as exc:
        story_log(
            logger,
            logging.WARNING,
            "hypothesis lifecycle count failed; telemetry partial",
            comp=COMP_ACTVTY,
            func="_count_hypothesis_lifecycle",
            stage="execute",
            step="count",
            outcome=OUTCOME_SKIPPED,
            tenant_id=str(tenant_id),
            investigation_id=investigation_id,
            event="hypothesis lifecycle count failed",
            error=str(exc),
        )
    return counts


async def _count_evidence_banked(ctx: Any, tenant_id: str, investigation_id: Any) -> int:
    """Banked evidence row count (fail-open to 0)."""
    try:
        repo: Any = getattr(ctx, "evidence_repo", None)
        if repo is None:
            return 0
        rows = await repo.find_by_investigation_id(tenant_id, investigation_id)
        return len(rows or [])
    except Exception as exc:
        story_log(
            logger,
            logging.WARNING,
            "evidence count failed; telemetry partial",
            comp=COMP_ACTVTY,
            func="_count_evidence_banked",
            stage="execute",
            step="count",
            outcome=OUTCOME_SKIPPED,
            tenant_id=str(tenant_id),
            investigation_id=investigation_id,
            event="evidence count failed",
            error=str(exc),
        )
        return 0


@dataclass
class TransitionInvestigationInput:
    tenant_id: str
    investigation_id: str
    target_status: str
    reason: str = ""
    # Domain actor for the audit trail (TO_DO-012): threaded through
    # _transition_via_path into transition_to/record_transition. Defaults to
    # SYSTEM for workflow-driven edges; callers acting for a user pass USER.
    actor: str = "SYSTEM"
    # Terminal failure record for workflow-error reconciliation: when the
    # target is FAILED and a code is present, it is stamped onto metadata
    # (see _stamp_failure) so the API reports the cause. Empty otherwise.
    failure_code: str = ""
    failure_reason: str = ""


@dataclass
class PublishEventInput:
    tenant_id: str
    investigation_id: str
    event: str


@dataclass
class RetrieveKnowledgeInput:
    tenant_id: str
    investigation_id: str
    kinds: list[str] | None = None
    # B-6: complaint text for reference-docs recall. None skips refdoc
    # recall (artifact legs still run); keeps legacy callers working.
    query: str | None = None


@dataclass
class CaptureKnowledgeInput:
    tenant_id: str
    investigation_id: str


def _get_ctx() -> Any:
    # Any justified: AppContext is assembled in bootstrap/api layers and
    # intentionally untyped at the worker boundary (see _idempotency_store_for).
    from investigation_agent_platform.api.dependencies import get_app_context

    return get_app_context()
