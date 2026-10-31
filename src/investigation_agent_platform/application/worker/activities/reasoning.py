# src/investigation_agent_platform/application/worker/activities/reasoning.py
"""Temporal activity thin adapters — each delegates to the application/domain layer via AppContext."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from uuid import UUID

from temporalio import activity

try:
    from temporalio.exceptions import ApplicationFailure  # type: ignore[attr-defined]
except ImportError:
    from temporalio.exceptions import (
        ApplicationError as ApplicationFailure,  # type: ignore[assignment, no-redef]
    )

logger = logging.getLogger(__name__)

#: Upper bound on a provider-supplied retry_after (seconds): a hostile or buggy
#: header must not park an activity retry for hours.
_MAX_RETRY_AFTER_SECONDS = 60.0

from investigation_agent_platform.application.worker.reasoning_failure import (
    KIND_TRANSIENT,
    TRANSIENT_FAILURE_TYPE,
)
from investigation_agent_platform.infrastructure.observability.tracing import traced

from .common import (
    COMP_ACTVTY,
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    OUTCOME_SKIPPED,
    OUTCOME_STARTED,
    _activity_extra,
    _activity_outcome,
    _application_failure_from_exc,
    _coerced_data,
    _decision_llm_usage,
    _hub_orientation_for,
    _latest_fuzzy_orientation,
    _story_debug,
    apply_effective_limits,
    elapsed_ms,
    overlay_evidence_budget,
    story_log,
    tick,
)
from .inputs import (
    CreateInvestigationInput,
    CreateInvestigationOutput,
    GenericActivityResult,
    ReasonInput,
    RetrieveEvidenceInput,
    _caller_edge_candidates_from_evidence,
    _get_ctx,
)


def _disposition_routing(investigation: Any) -> tuple[str | None, str | None]:
    """Extract §2 disposition routing (action + primary link) for workflow
    not-startable handling. Never raises: unparsable metadata yields Nones."""
    try:
        metadata = getattr(investigation, "metadata", None) or {}
        disp = metadata.get("disposition") if isinstance(metadata, dict) else None
        if not isinstance(disp, dict):
            return None, None
        action = disp.get("action")
        pid = disp.get("primary_target_id")
        return (
            str(action) if action else None,
            str(pid) if pid else None,
        )
    except Exception:
        return None, None


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def create_investigation_activity(
    params: CreateInvestigationInput,
) -> CreateInvestigationOutput:
    # TO_DO-011: not idempotency-wired — creation mints a new identity per
    # call so no deterministic key exists; the F-IDENTITY load path
    # (investigation_id set) is idempotent by lookup instead.
    t0 = tick()
    try:
        data = _coerced_data(
            CreateInvestigationInput, params, activity="create_investigation_activity"
        )
        params = CreateInvestigationInput(**data)
        story_log(
            logger,
            logging.INFO,
            "create_investigation_activity started",
            comp=COMP_ACTVTY,
            func="create_investigation_activity",
            stage="intake",
            step="create",
            outcome=OUTCOME_STARTED,
            tenant_id=params.tenant_id,
            investigation_id=params.investigation_id or "",
            event="create_investigation_activity started",
            inputs={"application_id": params.application_id},
            application_id=params.application_id,
        )
        ctx = _get_ctx()

        # F-IDENTITY: if the caller (API) already created this investigation,
        # load it instead of creating a second aggregate with a new ID. This
        # is the single most important identity invariant in the workflow:
        # the API's investigation_id, the Temporal workflow ID
        # (`wf-investigation-{investigation_id}`), and every downstream
        # activity/checkpoint/evidence/attribution record must refer to the
        # same investigation.
        if params.investigation_id:
            inv_repo: Any = getattr(ctx, "investigation_repo", None)
            if inv_repo is None:
                raise ApplicationFailure(
                    "Investigation repository is not configured; cannot load existing investigation",
                    type="InvestigationRepositoryUnavailable",
                    non_retryable=True,
                )
            existing_inv_id = UUID(params.investigation_id)
            existing = await inv_repo.get_by_id(params.tenant_id, existing_inv_id)
            if existing is None:
                raise ApplicationFailure(
                    f"Investigation {existing_inv_id} not found for tenant; "
                    "workflow cannot proceed under a different identity",
                    type="InvestigationNotFoundError",
                    non_retryable=True,
                )
            story_log(
                logger,
                logging.INFO,
                "create_investigation_activity completed",
                comp=COMP_ACTVTY,
                func="create_investigation_activity",
                stage="intake",
                step="create",
                outcome=OUTCOME_COMPLETED,
                tenant_id=params.tenant_id,
                investigation_id=str(existing.id),
                event="create_investigation_activity completed",
                outputs={"outcome": "loaded_existing", "status": existing.status.value},
                elapsed_ms=elapsed_ms(t0),
            )
            _disp, _pid = _disposition_routing(existing)
            return CreateInvestigationOutput(
                investigation_id=str(existing.id),
                status=existing.status.value,
                disposition_action=_disp,
                primary_target_id=_pid,
            )

        from investigation_agent_platform.domain.investigation.models import InvestigationRequest

        svc = ctx.create_investigation_service()
        req = InvestigationRequest(
            problem_description=params.description or "",
            application_id=params.application_id,
            session_id=params.session_id or "",
            requested_by=params.tenant_id,
        )
        inv = await svc.execute(req, tenant_id=params.tenant_id)
        story_log(
            logger,
            logging.INFO,
            "create_investigation_activity completed",
            comp=COMP_ACTVTY,
            func="create_investigation_activity",
            stage="intake",
            step="create",
            outcome=OUTCOME_COMPLETED,
            tenant_id=params.tenant_id,
            investigation_id=str(inv.id),
            event="create_investigation_activity completed",
            outputs={"outcome": "created", "status": inv.status.value},
            elapsed_ms=elapsed_ms(t0),
        )
        _disp, _pid = _disposition_routing(inv)
        return CreateInvestigationOutput(
            investigation_id=str(inv.id),
            status=inv.status.value,
            disposition_action=_disp,
            primary_target_id=_pid,
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        story_log(
            logger,
            logging.ERROR,
            "create_investigation_activity failed",
            comp=COMP_ACTVTY,
            func="create_investigation_activity",
            stage="intake",
            step="create",
            outcome=OUTCOME_FAILED,
            tenant_id=getattr(params, "tenant_id", "")
            if isinstance(params, CreateInvestigationInput)
            else "",
            investigation_id=getattr(params, "investigation_id", "")
            if isinstance(params, CreateInvestigationInput)
            else "",
            event="create_investigation_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def retrieve_evidence_activity(
    params: RetrieveEvidenceInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — this is a read path whose
    # side-effect writes dedupe on evidence fingerprint, and the workflow
    # loop intentionally re-queries every iteration expecting fresh results;
    # an activity-level replay key would freeze a point-in-time count.
    t0 = tick()
    data = _coerced_data(RetrieveEvidenceInput, params, activity="retrieve_evidence_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    story_log(
        logger,
        logging.INFO,
        "retrieve_evidence_activity started",
        comp=COMP_ACTVTY,
        func="retrieve_evidence_activity",
        stage="execute",
        step="search_runtime",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        investigation_id=inv_id_probe,
        event="retrieve_evidence_activity started",
    )
    try:
        tenant_id: str = str(data.get("tenant_id", ""))
        inv_id_str: str = str(data.get("investigation_id", ""))
        if not tenant_id or not inv_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and investigation_id required"
            )
        investigation_id = UUID(inv_id_str)
        ctx = _get_ctx()
        gateway: Any = getattr(ctx, "evidence_gateway", None)
        if gateway is None:
            # F-021: a missing evidence gateway is a configuration error, not a
            # zero-result success. The workflow must not proceed as though
            # retrieval happened.
            raise ApplicationFailure(
                "Evidence gateway is not configured; evidence retrieval cannot proceed",
                type="EVIDENCE_PROVIDER_UNAVAILABLE",
                non_retryable=True,
            )

        # F-022: build a bounded query scoped to the investigation's profile
        # (environment, time window, result cap) instead of a wildcard
        # full-environment scan.
        from datetime import UTC, datetime, timedelta

        from investigation_agent_platform.domain.evidence.requests import (
            RuntimeEvidenceRequest,
            TimeRange,
        )

        inv_repo: Any = getattr(ctx, "investigation_repo", None)
        profile_repo: Any = getattr(ctx, "profile_repo", None)
        investigation_obj: Any = None
        if inv_repo is not None:
            investigation_obj = await inv_repo.get_by_id(tenant_id, investigation_id)

        app_id: str = str(
            data.get("application_id")
            or (getattr(investigation_obj, "application_id", None) if investigation_obj else None)
            or "example-app"
        )
        environment = "production"
        window_seconds = 3600
        max_results = 100
        if profile_repo is not None:
            profile = await profile_repo.get_by_application_id(tenant_id, app_id)
            if profile is not None:
                environment = profile.environment
                window_seconds = profile.investigation.default_time_window
                # Part 9: RuntimeEvidenceRequest caps limit at the provider
                # ceiling (200); clamp here so a generous profile knob cannot
                # fail request validation (provider clamped identically before).
                max_results = min(profile.investigation.max_evidence_per_query, 200)

        now = datetime.now(UTC)
        req = RuntimeEvidenceRequest(
            environment=environment,
            time_range=TimeRange(start_time=now - timedelta(seconds=window_seconds), end_time=now),
            limit=max_results,
        )
        try:
            result = await gateway.search_runtime_evidence(tenant_id, investigation_id, app_id, req)
        except Exception as exc:
            from investigation_agent_platform.domain.common.exceptions import (
                SecurityPolicyViolationException,
            )

            if isinstance(exc, SecurityPolicyViolationException):
                # Tranche B doctrine: a code-only profile denies SEARCH_LOGS
                # by design — denial degrades to empty, never fails the run.
                # Provider/config errors below stay loud (F-021).
                story_log(
                    logger,
                    logging.WARNING,
                    "retrieve_evidence_activity denied by authorization; degrading to empty",
                    comp=COMP_ACTVTY,
                    func="retrieve_evidence_activity",
                    stage="execute",
                    step="search_runtime",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="retrieve_evidence_activity auth degraded",
                    outputs={"degraded": "unauthorized", "items_count": 0},
                    elapsed_ms=elapsed_ms(t0),
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                return GenericActivityResult(
                    success=True,
                    data={"items_count": 0, "total_count": 0, "degraded": "unauthorized"},
                )
            story_log(
                logger,
                logging.ERROR,
                "retrieve_evidence_activity gateway call failed",
                comp=COMP_ACTVTY,
                func="retrieve_evidence_activity",
                stage="execute",
                step="search_runtime",
                outcome=OUTCOME_FAILED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="retrieve_evidence_activity gateway call failed",
                elapsed_ms=elapsed_ms(t0),
                error=str(exc),
                error_type=type(exc).__name__,
                exc_info=True,
            )
            raise _application_failure_from_exc(exc) from exc
        story_log(
            logger,
            logging.INFO,
            "retrieve_evidence_activity completed",
            comp=COMP_ACTVTY,
            func="retrieve_evidence_activity",
            stage="execute",
            step="search_runtime",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            investigation_id=inv_id_str,
            event="retrieve_evidence_activity completed",
            outputs={"items_count": len(result.items), "total_count": result.total_count},
            elapsed_ms=elapsed_ms(t0),
            items_count=len(result.items),
            total_count=result.total_count,
        )
        return GenericActivityResult(
            success=True,
            data={"items_count": len(result.items), "total_count": result.total_count},
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        story_log(
            logger,
            logging.ERROR,
            "retrieve_evidence_activity failed",
            comp=COMP_ACTVTY,
            func="retrieve_evidence_activity",
            stage="execute",
            step="search_runtime",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="retrieve_evidence_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise _application_failure_from_exc(exc) from exc


def select_reasoning_evidence(items: Any, hypotheses: Any, limit: int = 200) -> list[Any]:
    """Phase 1.3: deterministic, claim-prioritized evidence hydration.

    The repository returns rows unordered, and ``InvestigationState.evidence``
    holds at most 200. Taking the first N therefore dropped cited or code rows
    arbitrarily. Priority: rows cited by a hypothesis, then code rows, then
    the newest of the rest; the final order is a stable
    (retrieved_at, observed_at, evidence_id) sort so retries see identical input.
    """

    def _key(e: Any) -> tuple[str, str, str]:
        return (
            str(getattr(e, "retrieved_at", "") or ""),
            str(getattr(e, "observed_at", "") or ""),
            str(getattr(e, "evidence_id", "") or ""),
        )

    cited: set[str] = set()
    for h in hypotheses or []:
        for attr in ("supporting_evidence_ids", "refuting_evidence_ids"):
            cited.update(str(i) for i in (getattr(h, attr, None) or []) if i)

    def _is_code(e: Any) -> bool:
        etype = str(getattr(getattr(e, "evidence_type", ""), "value", "") or "").upper()
        return "CODE" in etype or "EXCEPTION" in etype

    ordered = sorted(items or [], key=_key)
    chosen: list[Any] = []
    seen: set[str] = set()

    def _take(rows: list[Any]) -> None:
        for row in reversed(rows):  # newest first within each tier
            rid = str(getattr(row, "evidence_id", "") or "")
            if rid in seen or len(chosen) >= limit:
                continue
            seen.add(rid)
            chosen.append(row)

    _take([e for e in ordered if str(getattr(e, "evidence_id", "")) in cited])
    _take([e for e in ordered if _is_code(e)])
    _take(ordered)
    return sorted(chosen, key=_key)


def _s4_readiness(
    evidence_items: Any, investigation: Any, default_lookback_s: int = 3600
) -> dict[str, Any]:
    """P2 (S4): shape-aware readiness over the reasoner's evidence set.

    Computes qualify-per-row plus s4_ready(shape, …) from the banked rows,
    P1 anchors, and the question shape. Returns {} when undecidable so
    legacy behavior (count-based floor) is preserved. Never raises.
    """
    try:
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime

        from investigation_agent_platform.domain.analysis.question_shape import (
            classify_question_shape,
        )
        from investigation_agent_platform.domain.evidence.qualification import (
            QUALIFIER_VERSION,
            qualify_item,
            s4_ready,
        )
        from investigation_agent_platform.domain.investigation.models import (
            anchors_from_metadata,
            derive_working_window,
        )

        try:
            lookback = int(default_lookback_s or 3600)
        except (TypeError, ValueError):
            lookback = 3600
        anchors = anchors_from_metadata(getattr(investigation, "metadata", None))
        start, end, _ = derive_working_window(anchors, _datetime.now(_UTC), lookback)
        quals = [qualify_item(item, window=(start, end)) for item in (evidence_items or [])]
        request = getattr(investigation, "request", None)
        problem = str(getattr(request, "problem_description", "") or "")
        shape = classify_question_shape(problem)
        ready, missing, satisfied_by = s4_ready(
            shape,
            quals,
            window=(start, end),
            items=list(evidence_items or []),
            t_anchored=bool(anchors.onset_given),
        )
        layers: dict[str, int] = {}
        for qual in quals:
            if qual.qualified:
                layers[qual.layer] = layers.get(qual.layer, 0) + 1
        return {
            "s4_ready": bool(ready),
            "s4_missing": str(missing),
            "s4_satisfied_by": str(satisfied_by),
            "s4_shape": str(shape),
            "s4_qualified_layers": layers,
            "s4_qualifier_version": str(QUALIFIER_VERSION),
        }
    except Exception:
        return {}


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def reason_activity(params: ReasonInput) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — pure compute over persisted state,
    # no durable writes of its own; re-execution is safe by construction.
    t0 = tick()
    data = _coerced_data(ReasonInput, params, activity="reason_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    story_log(
        logger,
        logging.INFO,
        "reason_activity started",
        comp=COMP_ACTVTY,
        func="reason_activity",
        stage="reason",
        step="start",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        investigation_id=inv_id_probe,
        event="reason_activity started",
        inputs={"iteration": int(data.get("iteration", 0))},
        iteration=int(data.get("iteration", 0)),
    )
    # Debug story: reasoner input. Shows exactly what the model saw going
    # in — recent action history with outcomes is the loop's memory.
    _story_debug(
        tenant_id=tenant_id_probe,
        investigation_id=inv_id_probe,
        event="story.reason_in",
        iteration=int(data.get("iteration", 0)),
        recent_actions=list(data.get("recent_actions", []) or []),
        knowledge_orientation=list(data.get("knowledge_orientation", []) or []),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        iteration = int(data.get("iteration", 0))
        recent_actions = [dict(a) for a in (data.get("recent_actions", []) or [])]
        knowledge_orientation = [
            dict(n) for n in (data.get("knowledge_orientation", []) or []) if isinstance(n, dict)
        ]
        if not tenant_id or not inv_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and investigation_id required"
            )
        investigation_id = UUID(inv_id_str)
        ctx = _get_ctx()
        # Resolve ReasoningCoordinator: prefer ctx.reasoning_coordinator, else build one from
        # production configuration. Construction failure is a security/configuration error and
        # must never fall back to a permissive/allow-all safety policy (F-005).
        coordinator: Any = getattr(ctx, "reasoning_coordinator", None)
        if coordinator is None:
            from investigation_agent_platform.bootstrap import build_reasoning_coordinator
            from investigation_agent_platform.infrastructure.configuration.config import (
                load_application_config_from_env,
            )

            try:
                cfg = load_application_config_from_env()
                coordinator = build_reasoning_coordinator(cfg)
            except Exception as exc:
                story_log(
                    logger,
                    logging.ERROR,
                    "reason_activity: failed to construct reasoning coordinator; "
                    "refusing to process prompts with a permissive fallback",
                    comp=COMP_ACTVTY,
                    func="reason_activity",
                    stage="reason",
                    step="init_coordinator",
                    outcome=OUTCOME_FAILED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="reason_activity coordinator init failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    exc_info=True,
                )
                raise ApplicationFailure(
                    "Reasoning/safety policy could not be initialized; refusing to process",
                    type="SecurityPolicyInitializationError",
                    non_retryable=True,
                ) from exc

        # Build minimal InvestigationState for reasoning.
        attach = getattr(coordinator, "attach_profile_repo", None)
        if callable(attach):
            try:
                attach(getattr(ctx, "profile_repo", None))
            except Exception as exc:
                story_log(
                    logger,
                    logging.WARNING,
                    "reason profile attach failed",
                    comp=COMP_ACTVTY,
                    func="reason_activity",
                    stage="reason",
                    step="attach_profile",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="reason profile attach failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
        try:
            inv_repo: Any = getattr(ctx, "investigation_repo", None)
            investigation: Any = None
            if inv_repo is not None:
                investigation = await inv_repo.get_by_id(tenant_id, investigation_id)
            if investigation is None:
                # No persisted investigation — this is a genuine failure, not a
                # basis for fabricating a readiness score (F-006).
                raise ApplicationFailure(
                    f"Investigation {investigation_id} not found for tenant",
                    type="InvestigationNotFoundError",
                    non_retryable=True,
                )

            from investigation_agent_platform.domain.investigation.models import InvestigationState

            # Try to hydrate from checkpoint if available.
            checkpoint_repo: Any = getattr(ctx, "checkpoint_repo", None)
            state: InvestigationState | None = None
            if checkpoint_repo is not None:
                try:
                    snap = await checkpoint_repo.get_latest_checkpoint(tenant_id, investigation_id)
                    if snap is not None:
                        state = InvestigationState.model_validate(snap)
                except Exception as exc:
                    story_log(
                        logger,
                        logging.WARNING,
                        "reason_activity checkpoint hydration failed",
                        comp=COMP_ACTVTY,
                        func="reason_activity",
                        stage="reason",
                        step="hydrate_checkpoint",
                        outcome=OUTCOME_SKIPPED,
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="reason_activity checkpoint hydration failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
            if state is None:
                state = InvestigationState(investigation=investigation)

            # Enrich reasoning state from the repositories. Checkpoints (and
            # fresh states) carry the aggregate only — evidence, hypotheses,
            # findings, and timeline live in their own repos. Without this
            # the model reasons over an eternally empty evidence_records list:
            # it keeps gathering, never formulates, readiness stays 0, and
            # every run ends FAILED at the iteration cap.
            try:
                hyps: list[Any] = []
                hypothesis_repo: Any = getattr(ctx, "hypothesis_repo", None)
                if hypothesis_repo is not None:
                    hyps = list(
                        await hypothesis_repo.find_by_investigation_id(tenant_id, investigation_id)
                    )[:50]
                    state = state.model_copy(update={"active_hypotheses": hyps})
                evidence_repo: Any = getattr(ctx, "evidence_repo", None)
                if evidence_repo is not None:
                    items = await evidence_repo.find_by_investigation_id(
                        tenant_id, investigation_id
                    )
                    state = state.model_copy(
                        update={"evidence": select_reasoning_evidence(items, hyps)}
                    )
                finding_repo: Any = getattr(ctx, "finding_repo", None)
                if finding_repo is not None:
                    found, _ = await finding_repo.list_findings(
                        tenant_id, investigation_id, limit=50
                    )
                    state = state.model_copy(update={"findings": list(found)[:50]})
                timeline_repo: Any = getattr(ctx, "timeline_repo", None)
                if timeline_repo is not None:
                    events = await timeline_repo.find_by_investigation_id(
                        tenant_id, investigation_id
                    )
                    state = state.model_copy(update={"timeline": list(events)[-100:]})
            except Exception as exc:
                story_log(
                    logger,
                    logging.WARNING,
                    "reason_activity state enrichment failed; reasoning unassisted",
                    comp=COMP_ACTVTY,
                    func="reason_activity",
                    stage="reason",
                    step="enrich_state",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="reason_activity state enrichment failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

            # D4 fuzzy orientation: history-carried, newest wins (see
            # _latest_fuzzy_orientation). Threaded into reason(), whose
            # renderer already gates it as non-citable steering.
            _fuzzy_orientation = _latest_fuzzy_orientation(recent_actions)
            decision = await coordinator.reason(
                tenant_id,
                state,
                recent_actions=recent_actions,
                orientation_notes=knowledge_orientation,
                hub_orientation=await _hub_orientation_for(ctx, tenant_id, investigation),
                fuzzy_orientation=_fuzzy_orientation,
            )
            # Phase 3.4: an LLM outage is a platform failure, not a default
            # action. A REASONING_UNAVAILABLE decision previously flowed
            # through with action=None and the workflow dispatched QUERY_STATE
            # — a contentless loop that could end as causeless abstention.
            # Surface it explicitly; the workflow breaks with the cause.
            _decision_meta = getattr(decision, "metadata", None) or {}
            _unavailable_reason = str(_decision_meta.get("reason", "") or "").strip()
            if (
                str(_decision_meta.get("status", "") or "") == "REASONING_UNAVAILABLE"
                or str(getattr(decision, "reasoning_chain", "") or "") == "REASONING_UNAVAILABLE"
            ):
                story_log(
                    logger,
                    logging.WARNING,
                    "reason_activity reasoning unavailable; propagating typed failure",
                    comp=COMP_ACTVTY,
                    func="reason_activity",
                    stage="reason",
                    step="decide",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="reason_activity reasoning unavailable",
                    outputs={"reason": _unavailable_reason[:1000]},
                    elapsed_ms=elapsed_ms(t0),
                    error=_unavailable_reason[:500],
                )
                _failure_kind = str(_decision_meta.get("failure_kind", "") or "other")
                if _failure_kind == KIND_TRANSIENT:
                    # H7: a transient outage (timeout / 5xx / throttle) must be
                    # retried by Temporal, not returned as success and turned
                    # into a terminal failure. A throttle's retry_after becomes
                    # the server-side retry delay (replay-safe; never a sleep).
                    _delay: timedelta | None = None
                    _hint = _decision_meta.get("retry_after")
                    if (
                        isinstance(_hint, (int, float))
                        and not isinstance(_hint, bool)
                        and _hint > 0
                    ):
                        _delay = timedelta(seconds=min(float(_hint), _MAX_RETRY_AFTER_SECONDS))
                    raise ApplicationFailure(
                        _unavailable_reason[:500] or "reasoning provider unavailable",
                        type=TRANSIENT_FAILURE_TYPE,
                        non_retryable=False,
                        next_retry_delay=_delay,
                    )
                return GenericActivityResult(
                    success=True,
                    data={
                        "reasoning_unavailable": True,
                        "reason": _unavailable_reason[:1000],
                        "failure_kind": _failure_kind,
                        # H6: tokens spent on the failed attempts still count.
                        "llm_usage": dict(_decision_meta.get("llm_usage") or {}),
                        "conclusion_readiness": 0.0,
                        "observations": [],
                        "iteration": iteration,
                    },
                )
            # Choreography: the workflow dispatches on next_action /
            # action_parameters — they must be propagated here, not dropped.
            # (Previously only readiness/observations were returned, so the
            # workflow always fell back to QUERY_STATE and reasoning was
            # effectively severed from execution.)
            action = getattr(decision, "action", None)
            action_type_value = getattr(getattr(action, "action_type", None), "value", None)
            hypotheses_out = list(getattr(decision, "hypotheses_evaluated", None) or [])
            disposition = getattr(getattr(decision, "disposition", None), "value", None)
            defer_gathering = getattr(decision, "defer_gathering", None)
            # P0-3: persist model-reported contradictions instead of dropping
            # them — verification and the conclusion gate read them back from
            # the repo. Best-effort: persistence trouble must never fail
            # reasoning itself. Statements dedupe against existing rows.
            try:
                contradiction_repo: Any = getattr(ctx, "contradiction_repo", None)
                reported = [
                    str(s or "").strip()[:2048]
                    for s in (getattr(decision, "contradictions_found", None) or [])
                ]
                reported = [s for s in reported if s]
                if contradiction_repo is not None and reported:
                    from investigation_agent_platform.application.investigation.verification import (
                        Contradiction,
                        ContradictionSeverity,
                    )

                    existing = await contradiction_repo.find_by_investigation_id(
                        tenant_id, investigation_id
                    )
                    known_statements = {
                        str(getattr(c, "statement", "")).strip() for c in (existing or [])
                    }
                    for statement in reported:
                        if statement in known_statements:
                            continue
                        known_statements.add(statement)
                        await contradiction_repo.save(
                            tenant_id,
                            Contradiction(
                                statement=statement,
                                severity=ContradictionSeverity.MEDIUM,
                            ),
                            investigation_id,
                        )
            except Exception as exc:
                story_log(
                    logger,
                    logging.WARNING,
                    "contradiction persistence failed; continuing unassisted",
                    comp=COMP_ACTVTY,
                    func="reason_activity",
                    stage="reason",
                    step="persist_contradictions",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="contradiction persistence failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            # Operational telemetry: type names and counts only, never prompt
            # or payload values (prompt content stays out of logs). Values ride
            # as extra keys (TO_DO-007/019); the message keeps lazy %-style.
            story_log(
                logger,
                logging.WARNING,
                "reason_activity decision action=%s readiness=%s hypotheses=%s observations=%s",
                action_type_value or "QUERY_STATE",
                decision.conclusion_readiness,
                len(hypotheses_out),
                len(decision.observations or []),
                comp=COMP_ACTVTY,
                func="reason_activity",
                stage="reason",
                step="decide",
                outcome=OUTCOME_COMPLETED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="reason_activity decision",
                outputs={
                    "action": action_type_value or "QUERY_STATE",
                    "readiness": decision.conclusion_readiness,
                    "hypotheses_count": len(hypotheses_out),
                    "observations_count": len(decision.observations or []),
                },
                elapsed_ms=elapsed_ms(t0),
                action=action_type_value or "QUERY_STATE",
                readiness=decision.conclusion_readiness,
                hypotheses_count=len(hypotheses_out),
                observations_count=len(decision.observations or []),
            )
            story_log(
                logger,
                logging.INFO,
                "reason_activity completed",
                comp=COMP_ACTVTY,
                func="reason_activity",
                stage="reason",
                step="complete",
                outcome=OUTCOME_COMPLETED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="reason_activity completed",
                outputs={"outcome": "reasoned", "iteration": iteration},
                elapsed_ms=elapsed_ms(t0),
                iteration=iteration,
            )
            # Debug story: full reason-out payload (proposal + defer + claims).
            # The workflow may override next_action via forcing rules; the
            # delta between this proposal and execute_action_activity input
            # is the forcing story. Sanitized + truncated in _story_debug.
            _story_debug(
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="story.reason_out",
                iteration=iteration,
                disposition=disposition,
                next_action=action_type_value or "QUERY_STATE",
                action_parameters=dict(getattr(action, "parameters", None) or {}),
                defer_gathering=(
                    defer_gathering.model_dump(mode="json") if defer_gathering is not None else None
                ),
                claims=list(getattr(decision, "claims", None) or []),
                hypotheses_evaluated=hypotheses_out,
                conclusion_readiness=decision.conclusion_readiness,
                observations=list(decision.observations or []),
            )
            # Token budget ledger: accumulate this call into the hydrated
            # counters and route to conclude when the investigation exceeds
            # its LLM token budget — same routing philosophy as the tool and
            # evidence budgets (overshoot is bounded by a single call).
            # Usage also rides the result so the workflow can persist
            # cumulative totals into checkpoints.
            from investigation_agent_platform.domain.investigation.models import (
                InvestigationLimits,
            )

            usage = _decision_llm_usage(decision)
            call_tokens = int(usage["prompt_tokens"]) + int(usage["completion_tokens"])
            call_cost = float(usage["cost_usd"])
            cumulative_tokens = int(getattr(state, "llm_tokens_used", 0) or 0) + call_tokens
            limits = InvestigationLimits()
            try:
                profile_repo = getattr(ctx, "profile_repo", None)
                if profile_repo is not None:
                    _budget_profile = await profile_repo.get_by_application_id(
                        tenant_id, state.investigation.application_id
                    )
                    if _budget_profile is not None:
                        limits = overlay_evidence_budget(limits, _budget_profile)
            except Exception:
                pass
            limits = apply_effective_limits(limits, state.investigation)
            # P2 (S4): shape-aware readiness over the reasoner's evidence
            # set. Flows into reason_data; the floor forcing consults it
            # instead of raw row counts. Fail-open: {} preserves legacy.
            _s4_lookback = 3600
            try:
                _s4_cfg = getattr(_budget_profile, "investigation", None)
                _s4_lb = getattr(_s4_cfg, "default_time_window", None)
                if isinstance(_s4_lb, int) and not isinstance(_s4_lb, bool) and _s4_lb > 0:
                    _s4_lookback = _s4_lb
            except Exception:
                pass
            _s4 = _s4_readiness(getattr(state, "evidence", []), state.investigation, _s4_lookback)
            if cumulative_tokens >= limits.max_llm_tokens:
                story_log(
                    logger,
                    logging.WARNING,
                    "reason_activity token budget exhausted; routing to conclude",
                    comp=COMP_ACTVTY,
                    func="reason_activity",
                    stage="reason",
                    step="check_budget",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="reason_activity token budget exhausted",
                    outputs={
                        "cumulative_tokens": cumulative_tokens,
                        "limit": limits.max_llm_tokens,
                    },
                    elapsed_ms=elapsed_ms(t0),
                    cumulative_tokens=cumulative_tokens,
                    limit=limits.max_llm_tokens,
                )
                return GenericActivityResult(
                    success=True,
                    data={
                        "budget_exceeded": True,
                        "llm_tokens": call_tokens,
                        "llm_cost": call_cost,
                        "limit": limits.max_llm_tokens,
                    },
                )
            return GenericActivityResult(
                success=True,
                data={
                    "conclusion_readiness": decision.conclusion_readiness,
                    "observations": list(decision.observations or []),
                    "iteration": iteration,
                    "next_action": action_type_value or "QUERY_STATE",
                    "action_parameters": dict(getattr(action, "parameters", None) or {}),
                    "hypotheses_evaluated": hypotheses_out,
                    "disposition": disposition,
                    "defer_gathering": (
                        defer_gathering.model_dump(mode="json")
                        if defer_gathering is not None
                        else None
                    ),
                    # Phase 2.1: typed claims with per-claim evidence binding.
                    "claims": [
                        c.model_dump(mode="json")
                        for c in (getattr(decision, "claims", None) or [])
                        if hasattr(c, "model_dump")
                    ],
                    "caller_edge_candidates": _caller_edge_candidates_from_evidence(state.evidence),
                    "evidence": [
                        {
                            "evidence_id": str(getattr(item, "evidence_id", "") or ""),
                            "tenant_id": str(getattr(item, "tenant_id", "") or ""),
                            "investigation_id": str(getattr(item, "investigation_id", "") or ""),
                            "attributes": dict(getattr(item, "attributes", {}) or {}),
                            "provenance": {
                                "tenant_id": str(
                                    getattr(getattr(item, "provenance", None), "tenant_id", "")
                                    or ""
                                ),
                                "investigation_id": str(
                                    getattr(
                                        getattr(item, "provenance", None),
                                        "investigation_id",
                                        "",
                                    )
                                    or ""
                                ),
                                "operation": str(
                                    getattr(
                                        getattr(
                                            getattr(item, "provenance", None),
                                            "query_fingerprint",
                                            None,
                                        ),
                                        "operation",
                                        "",
                                    )
                                    or ""
                                ),
                                "file_path": str(
                                    getattr(
                                        getattr(
                                            getattr(item, "provenance", None),
                                            "source_location",
                                            None,
                                        ),
                                        "file_path",
                                        "",
                                    )
                                    or ""
                                ),
                                "line_start": getattr(
                                    getattr(
                                        getattr(item, "provenance", None),
                                        "source_location",
                                        None,
                                    ),
                                    "line_start",
                                    None,
                                ),
                                "line_end": getattr(
                                    getattr(
                                        getattr(item, "provenance", None),
                                        "source_location",
                                        None,
                                    ),
                                    "line_end",
                                    None,
                                ),
                                "revision": str(
                                    getattr(
                                        getattr(
                                            getattr(item, "provenance", None),
                                            "source_location",
                                            None,
                                        ),
                                        "revision",
                                        "",
                                    )
                                    or ""
                                ),
                            },
                        }
                        for item in list(getattr(state, "evidence", []) or [])[:50]
                    ],
                    # P0-2: propagate the full decision surface — previously
                    # contradictions/gaps/rationale were dropped here, so
                    # verification always received empty lists downstream.
                    "contradictions_found": list(
                        getattr(decision, "contradictions_found", None) or []
                    ),
                    "information_gaps": list(getattr(decision, "information_gaps", None) or []),
                    "reasoning_chain": str(getattr(decision, "reasoning_chain", "") or "")[:2000],
                    "llm_usage": usage,
                    "llm_tokens": call_tokens,
                    "llm_cost": call_cost,
                    # P2 (S4): shape-aware readiness for the floor forcing.
                    # Absent (legacy/undecidable) preserves count-based floor.
                    "s4_ready": _s4.get("s4_ready"),
                    "s4_missing": _s4.get("s4_missing"),
                    "s4_satisfied_by": _s4.get("s4_satisfied_by"),
                    "s4_shape": _s4.get("s4_shape"),
                    "s4_qualified_layers": _s4.get("s4_qualified_layers"),
                    "s4_qualifier_version": _s4.get("s4_qualifier_version"),
                },
            )
        except ApplicationFailure:
            # Already typed (H7: the retryable transient-reasoning failure
            # raised above); re-wrapping would turn it non-retryable.
            raise
        except Exception as exc:
            story_log(
                logger,
                logging.ERROR,
                "reason_activity reasoning call failed",
                comp=COMP_ACTVTY,
                func="reason_activity",
                stage="reason",
                step="reason",
                outcome=OUTCOME_FAILED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="reason_activity reasoning call failed",
                error=str(exc),
                error_type=type(exc).__name__,
                exc_info=True,
            )
            raise _application_failure_from_exc(exc) from exc
    except ApplicationFailure:
        raise
    except Exception as exc:
        story_log(
            logger,
            logging.ERROR,
            "reason_activity failed",
            comp=COMP_ACTVTY,
            func="reason_activity",
            stage="reason",
            step="reason",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="reason_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise _application_failure_from_exc(exc) from exc
