# src/investigation_agent_platform/application/worker/activities/lifecycle.py
"""Temporal activity thin adapters — each delegates to the application/domain layer via AppContext."""

from __future__ import annotations

import logging
from collections import deque
from datetime import UTC, datetime
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

from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError
from investigation_agent_platform.domain.proof.definition_shadow import (
    ShadowError,
    attach_shadow_observations,
    pair_receipt,
    resolve_definition_mode,
    run_shadow_turn,
    summarize_shadow_definitions,
    telemetry_omission_flagged,
)
from investigation_agent_platform.infrastructure.observability.tracing import traced

from .common import (
    COMP_ACTVTY,
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    OUTCOME_SKIPPED,
    OUTCOME_STARTED,
    _abandon_reservation,
    _activity_extra,
    _activity_outcome,
    _application_failure_from_exc,
    _coerced_data,
    _complete_reservation,
    _domain_transition_graph,
    _emit_telemetry,
    _limitation_kind,
    _normalize_actor,
    _reserve_or_replay,
    _story_debug,
    elapsed_ms,
    story_log,
    tick,
)
from .execution import _fuse_journey_findings
from .inputs import (
    CheckpointInput,
    ConcludeInvestigationInput,
    GenericActivityResult,
    PublishEventInput,
    TransitionInvestigationInput,
    _count_evidence_banked,
    _count_hypothesis_lifecycle,
    _derive_loop_telemetry,
    _failure_code_for,
    _get_ctx,
    _stamp_failure,
    _stamp_loop_telemetry,
)
from .verification import _load_definition_dispatch_table


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def checkpoint_activity(params: CheckpointInput) -> GenericActivityResult:
    t0 = tick()
    data = _coerced_data(CheckpointInput, params, activity="checkpoint_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    story_log(
        logger,
        logging.INFO,
        "checkpoint_activity started",
        comp=COMP_ACTVTY,
        func="checkpoint_activity",
        stage="persist",
        step="checkpoint",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        investigation_id=inv_id_probe,
        event="checkpoint_activity started",
        inputs={"step": data.get("step", 0)},
        checkpoint_step=data.get("step", 0),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        step = int(data.get("step", 0))
        state_snapshot: dict[str, Any] | None = data.get("state_snapshot")
        usage_totals = data.get("usage_totals") or {}
        if not tenant_id or not inv_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and investigation_id required"
            )
        investigation_id = UUID(inv_id_str)
        ctx = _get_ctx()

        # TO_DO-011: idempotency-wired — (investigation_id, step) is the
        # deterministic key, so a retried checkpoint step cannot duplicate
        # the snapshot row.
        _idem_active_key: str | None = None
        _idem_operation = "activity:checkpoint"
        _idem_key = f"checkpoint:{inv_id_str}:{step}"
        _replayed, _idem_active_key = await _reserve_or_replay(
            ctx=ctx,
            tenant_id=tenant_id,
            key=_idem_key,
            request_payload={
                "tenant_id": tenant_id,
                "investigation_id": inv_id_str,
                "step": step,
                "snapshot_present": state_snapshot is not None,
            },
            operation=_idem_operation,
            activity="checkpoint_activity",
            investigation_id=inv_id_str,
        )
        if _replayed is not None:
            return GenericActivityResult(
                success=bool(_replayed.get("success", True)),
                data=dict(_replayed.get("data") or {}),
                error=_replayed.get("error"),
            )

        checkpoint_repo: Any = getattr(ctx, "checkpoint_repo", None)
        # Also try _InMemoryCheckpointRepository attached via resume service pattern.
        if checkpoint_repo is None:
            # No repo wired — treat as no-op but succeed so workflow progresses.
            story_log(
                logger,
                logging.ERROR,
                "checkpoint_activity repo unwired; checkpoint not persisted",
                comp=COMP_ACTVTY,
                func="checkpoint_activity",
                stage="persist",
                step="checkpoint",
                outcome=OUTCOME_FAILED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="checkpoint_activity repo unwired",
                outputs={"step": step},
                checkpoint_step=step,
            )
            if _idem_active_key is not None:
                await _complete_reservation(
                    ctx=ctx,
                    tenant_id=tenant_id,
                    key=_idem_active_key,
                    operation=_idem_operation,
                    response={"success": True, "data": {"step": step, "noop": True}},
                    activity="checkpoint_activity",
                    investigation_id=inv_id_str,
                )
            return GenericActivityResult(success=True, data={"step": step})

        # Build snapshot from investigation if not supplied.
        if state_snapshot is None:
            inv_repo: Any = getattr(ctx, "investigation_repo", None)
            if inv_repo is not None:
                try:
                    inv = await inv_repo.get_by_id(tenant_id, investigation_id)
                    if inv is not None:
                        from investigation_agent_platform.domain.investigation.models import (
                            InvestigationState,
                        )

                        st = InvestigationState(investigation=inv)
                        # Carry token counters forward: fresh states reset
                        # them, which would unbound cumulative LLM spend.
                        # Best-effort — a missing/stale checkpoint degrades to
                        # zero, never breaks checkpointing itself.
                        carried_tokens = 0
                        carried_cost = 0.0
                        try:
                            prior = await checkpoint_repo.get_latest_checkpoint(
                                tenant_id, investigation_id
                            )
                            if isinstance(prior, dict):
                                carried_tokens = max(0, int(prior.get("llm_tokens_used", 0) or 0))
                                carried_cost = max(
                                    0.0,
                                    float(prior.get("llm_cost_usd", 0.0) or 0.0),
                                )
                        except Exception:
                            pass
                        if isinstance(usage_totals, dict) and usage_totals:
                            try:
                                carried_tokens = max(0, int(usage_totals.get("llm_tokens", 0) or 0))
                                carried_cost = max(
                                    0.0, float(usage_totals.get("llm_cost", 0.0) or 0.0)
                                )
                            except (TypeError, ValueError):
                                pass
                        st = st.model_copy(
                            update={
                                "llm_tokens_used": carried_tokens,
                                "llm_cost_usd": carried_cost,
                            }
                        )
                        state_snapshot = st.model_dump(mode="json")
                    else:
                        state_snapshot = {"step": step}
                except Exception as exc:
                    story_log(
                        logger,
                        logging.ERROR,
                        "checkpoint_activity snapshot build failed",
                        comp=COMP_ACTVTY,
                        func="checkpoint_activity",
                        stage="persist",
                        step="build_snapshot",
                        outcome=OUTCOME_FAILED,
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="checkpoint_activity snapshot build failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                        exc_info=True,
                        checkpoint_step=step,
                    )
                    state_snapshot = {"step": step}
            else:
                state_snapshot = {"step": step}

        await checkpoint_repo.save_checkpoint(
            tenant_id, investigation_id, step, state_snapshot or {"step": step}
        )
        if _idem_active_key is not None:
            await _complete_reservation(
                ctx=ctx,
                tenant_id=tenant_id,
                key=_idem_active_key,
                operation=_idem_operation,
                response={"success": True, "data": {"step": step}},
                activity="checkpoint_activity",
                investigation_id=inv_id_str,
            )
        story_log(
            logger,
            logging.INFO,
            "checkpoint_activity completed",
            comp=COMP_ACTVTY,
            func="checkpoint_activity",
            stage="persist",
            step="checkpoint",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            investigation_id=inv_id_str,
            event="checkpoint_activity completed",
            outputs={"outcome": "checkpointed", "step": step},
            elapsed_ms=elapsed_ms(t0),
            checkpoint_step=step,
        )
        return GenericActivityResult(success=True, data={"step": step})
    except ApplicationFailure:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="checkpoint_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        raise
    except Exception as exc:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="checkpoint_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        story_log(
            logger,
            logging.ERROR,
            "checkpoint_activity failed",
            comp=COMP_ACTVTY,
            func="checkpoint_activity",
            stage="persist",
            step="checkpoint",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="checkpoint_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise _application_failure_from_exc(exc) from exc


def _find_transition_path(
    start: Any, target: Any, transitions: dict[Any, set[Any]]
) -> list[Any] | None:
    """BFS shortest path for graceful shutdown; returns list of statuses including start->target.

    ``Any`` is justified: nodes are domain InvestigationStatus enums (or their
    string values) resolved lazily; the helper stays decoupled from the domain
    import graph.
    """
    queue: deque[list[Any]] = deque([[start]])
    visited: set[Any] = {start}
    while queue:
        path = queue.popleft()
        cur = path[-1]
        if cur == target:
            return path
        for nxt in transitions.get(cur, set()):
            if nxt not in visited:
                visited.add(nxt)
                queue.append(path + [nxt])
    return None


async def _transition_via_path(
    tenant_id: str,
    inv: Any,
    target: Any,
    reason: str,
    inv_repo: Any,
    transition_repo: Any | None = None,
    timeline_repo: Any | None = None,
    *,
    actor: Any = None,
) -> tuple[Any, bool]:
    """Walk the domain graph step-by-step, persisting each transition.

    Every edge is recorded to transition_repo (when available) in addition
    to saving the aggregate — previously the audit trail only captured
    API-driven transitions (e.g. cancel) while the entire workflow-driven
    lifecycle was invisible in investigation_transitions.

    Every edge also appends a STATE_CHANGE timeline event (when available):
    previously nothing on the live path ever wrote timeline_events, so the
    timeline tab could only ever render seeded rows.

    Returns ``(updated_investigation, audit_degraded)``: ``audit_degraded``
    is True when any audit write was skipped or failed. Skips and failures
    are logged loudly at error level (TO_DO-012 — no silent skip) but never
    break the lifecycle itself.

    The path-finding adjacency is derived from the domain single source via
    ``_domain_transition_graph`` (TO_DO-013) — there is no duplicated edge
    table here to drift. The domain ``actor`` is threaded into both
    ``transition_to`` and ``record_transition`` (TO_DO-012).
    """
    graph = _domain_transition_graph()
    resolved_actor = _normalize_actor(actor)
    actor_value = getattr(resolved_actor, "value", str(resolved_actor))
    audit_degraded = False
    inv_id_str = str(getattr(inv, "id", ""))

    async def _record_edge(from_status: Any, to_status: Any) -> None:
        nonlocal audit_degraded
        if transition_repo is None:
            audit_degraded = True
            story_log(
                logger,
                logging.ERROR,
                "transition audit repo unwired; edge %s -> %s not recorded",
                getattr(from_status, "value", str(from_status)),
                getattr(to_status, "value", str(to_status)),
                comp=COMP_ACTVTY,
                func="_transition_via_path",
                stage="persist",
                step="record_edge",
                outcome=OUTCOME_FAILED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="transition audit repo unwired",
                from_status=getattr(from_status, "value", str(from_status)),
                to_status=getattr(to_status, "value", str(to_status)),
            )
            return
        try:
            try:
                await transition_repo.record_transition(
                    tenant_id=tenant_id,
                    investigation_id=inv.id,
                    from_state=getattr(from_status, "value", str(from_status)),
                    to_state=getattr(to_status, "value", str(to_status)),
                    reason=reason,
                    actor=actor_value,
                )
            except TypeError:
                # Wave 3 owns the repository signature: the SQL implementation
                # still hardcodes actor="SYSTEM" while the port carries the new
                # actor kwarg — fall back loudly rather than losing the row.
                await transition_repo.record_transition(
                    tenant_id=tenant_id,
                    investigation_id=inv.id,
                    from_state=getattr(from_status, "value", str(from_status)),
                    to_state=getattr(to_status, "value", str(to_status)),
                    reason=reason,
                )
                audit_degraded = True
                story_log(
                    logger,
                    logging.ERROR,
                    "transition audit actor downgraded; repo signature predates actor",
                    comp=COMP_ACTVTY,
                    func="_transition_via_path",
                    stage="persist",
                    step="record_edge",
                    outcome=OUTCOME_FAILED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="transition audit actor downgraded",
                    actor=actor_value,
                )
        except Exception as exc:
            # Audit recording must never break the lifecycle itself.
            audit_degraded = True
            story_log(
                logger,
                logging.ERROR,
                "transition audit record failed",
                comp=COMP_ACTVTY,
                func="_transition_via_path",
                stage="persist",
                step="record_edge",
                outcome=OUTCOME_FAILED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="transition audit record failed",
                error=str(exc),
                error_type=type(exc).__name__,
                exc_info=True,
            )

    async def _append_timeline(from_status: Any, to_status: Any) -> None:
        nonlocal audit_degraded
        if timeline_repo is None:
            audit_degraded = True
            story_log(
                logger,
                logging.ERROR,
                "timeline repo unwired; STATE_CHANGE event not appended",
                comp=COMP_ACTVTY,
                func="_transition_via_path",
                stage="persist",
                step="append_timeline",
                outcome=OUTCOME_FAILED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="timeline repo unwired",
                from_status=getattr(from_status, "value", str(from_status)),
                to_status=getattr(to_status, "value", str(to_status)),
            )
            return
        try:
            from investigation_agent_platform.domain.timeline.models import (
                TimelineEvent,
                TimelineEventType,
            )

            existing = await timeline_repo.find_by_investigation_id(tenant_id, inv.id)
            seq = len(existing or [])
            from_value = getattr(from_status, "value", str(from_status))
            to_value = getattr(to_status, "value", str(to_status))
            await timeline_repo.append(
                tenant_id,
                TimelineEvent(
                    tenant_id=tenant_id,
                    investigation_id=inv.id,
                    sequence_number=seq,
                    timestamp=datetime.now(UTC),
                    event_type=TimelineEventType.STATE_CHANGE,
                    description=f"Status {from_value} -> {to_value}: {reason}",
                ),
                inv.id,
            )
        except Exception as exc:
            # Timeline is observability: never break the lifecycle.
            audit_degraded = True
            story_log(
                logger,
                logging.ERROR,
                "timeline append failed",
                comp=COMP_ACTVTY,
                func="_transition_via_path",
                stage="persist",
                step="append_timeline",
                outcome=OUTCOME_FAILED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="timeline append failed",
                error=str(exc),
                error_type=type(exc).__name__,
                exc_info=True,
            )

    path = _find_transition_path(inv.status, target, graph)

    if path is None:
        # No path - try direct transition to surface proper exception
        updated, _ = inv.transition_to(target, resolved_actor, reason)
        await inv_repo.save(tenant_id, updated, expected_version=inv.version)
        await _record_edge(inv.status, target)
        await _append_timeline(inv.status, target)
        return updated, audit_degraded
    current = inv
    # Walk edges excluding start node
    for nxt in path[1:]:
        updated, _ = current.transition_to(nxt, resolved_actor, reason)
        await inv_repo.save(tenant_id, updated, expected_version=current.version)
        await _record_edge(current.status, nxt)
        await _append_timeline(current.status, nxt)
        current = updated
    return current, audit_degraded


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def transition_investigation_activity(
    params: TransitionInvestigationInput,
) -> GenericActivityResult:
    t0 = tick()
    data = _coerced_data(
        TransitionInvestigationInput, params, activity="transition_investigation_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    story_log(
        logger,
        logging.INFO,
        "transition_investigation_activity started",
        comp=COMP_ACTVTY,
        func="transition_investigation_activity",
        stage="execute",
        step="transition",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        investigation_id=inv_id_probe,
        event="transition_investigation_activity started",
        inputs={"target_status": str(data.get("target_status", ""))},
        target_status=str(data.get("target_status", "")),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        target_status = str(data.get("target_status", ""))
        reason = str(data.get("reason", "")) or f"Transition to {target_status}"
        actor = str(data.get("actor", "SYSTEM")) or "SYSTEM"
        failure_code = str(data.get("failure_code", "") or "")
        failure_reason = str(data.get("failure_reason", "") or "")
        if not tenant_id or not inv_id_str or not target_status:
            return GenericActivityResult(
                success=False, error="tenant_id, investigation_id and target_status required"
            )
        investigation_id = UUID(inv_id_str)
        ctx = _get_ctx()
        inv_repo: Any = getattr(ctx, "investigation_repo", None)
        if inv_repo is None:
            story_log(
                logger,
                logging.ERROR,
                "transition_investigation_activity repo unwired; transition not applied",
                comp=COMP_ACTVTY,
                func="transition_investigation_activity",
                stage="execute",
                step="transition",
                outcome=OUTCOME_FAILED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="transition_investigation_activity repo unwired",
                target_status=target_status,
            )
            return GenericActivityResult(success=True, data={"status": target_status, "noop": True})
        inv = await inv_repo.get_by_id(tenant_id, investigation_id)
        if inv is None:
            return GenericActivityResult(success=False, error="investigation not found")
        from investigation_agent_platform.domain.investigation.models import InvestigationStatus

        try:
            target = InvestigationStatus(target_status)
        except ValueError:
            return GenericActivityResult(success=False, error=f"unknown status {target_status}")
        if inv.status == target:
            return GenericActivityResult(
                success=True, data={"status": target.value, "already": True}
            )

        # TO_DO-011: idempotency-wired — (investigation_id, target, version)
        # is deterministic: a retry after a lost response either replays the
        # stored outcome or no-ops on "already" when the version advanced.
        _idem_active_key: str | None = None
        _idem_operation = "activity:transition"
        _idem_key = f"transition:{inv_id_str}:{target.value}:{inv.version}"
        _replayed, _idem_active_key = await _reserve_or_replay(
            ctx=ctx,
            tenant_id=tenant_id,
            key=_idem_key,
            request_payload={
                "tenant_id": tenant_id,
                "investigation_id": inv_id_str,
                "target": target.value,
                "version": inv.version,
                "reason": reason,
                "actor": actor,
            },
            operation=_idem_operation,
            activity="transition_investigation_activity",
            investigation_id=inv_id_str,
        )
        if _replayed is not None:
            return GenericActivityResult(
                success=bool(_replayed.get("success", True)),
                data=dict(_replayed.get("data") or {"status": target.value}),
                error=_replayed.get("error"),
            )

        transition_inv = inv
        transition_reason = reason
        if target == InvestigationStatus.FAILED and failure_code:
            transition_inv = _stamp_failure(inv, failure_code, failure_reason or reason)
            transition_reason = f"{reason} ({failure_code})"
        _, audit_degraded = await _transition_via_path(
            tenant_id,
            transition_inv,
            target,
            transition_reason,
            inv_repo,
            getattr(ctx, "transition_repo", None),
            getattr(ctx, "timeline_repo", None),
            actor=actor,
        )
        if _idem_active_key is not None:
            await _complete_reservation(
                ctx=ctx,
                tenant_id=tenant_id,
                key=_idem_active_key,
                operation=_idem_operation,
                response={
                    "success": True,
                    "data": {"status": target.value, "audit_degraded": audit_degraded},
                },
                activity="transition_investigation_activity",
                investigation_id=inv_id_str,
            )
        story_log(
            logger,
            logging.INFO,
            "transition_investigation_activity completed",
            comp=COMP_ACTVTY,
            func="transition_investigation_activity",
            stage="execute",
            step="transition",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            investigation_id=inv_id_str,
            event="transition_investigation_activity completed",
            outputs={"outcome": "transitioned", "target_status": target.value},
            elapsed_ms=elapsed_ms(t0),
            target_status=target.value,
            actor=actor,
            audit_degraded=audit_degraded,
        )
        return GenericActivityResult(
            success=True, data={"status": target.value, "audit_degraded": audit_degraded}
        )
    except ApplicationFailure:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="transition_investigation_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        raise
    except Exception as exc:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="transition_investigation_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        story_log(
            logger,
            logging.ERROR,
            "transition_investigation_activity failed",
            comp=COMP_ACTVTY,
            func="transition_investigation_activity",
            stage="execute",
            step="transition",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="transition_investigation_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise _application_failure_from_exc(exc) from exc


def _caller_edge_causal_chain(candidate: Any, evidence_by_id: dict[Any, Any]) -> list[Any]:
    """Synthesize causal steps from verified CALL_EDGE assessments.

    A verified call edge is itself a causal step: the caller invokes the
    callee at a bound call site. Caller-enumeration answers rarely carry
    explicit VERIFY_HYPOTHESIS causal steps, so without this synthesis
    caller-edge-validated conclusions crash on the ROOT_CAUSE
    causal_chain invariant (live L2: VERIFIED then WORKFLOW_ERROR).
    Only bound assessments (evidence present in this investigation)
    qualify; anything else is skipped, never invented.
    """
    from investigation_agent_platform.application.investigation.verification import (
        CausalRelationship,
    )
    from investigation_agent_platform.domain.hypothesis.models import ClaimKind

    chain: list[Any] = []
    for _assessment in getattr(candidate, "assessments", None) or []:
        if getattr(_assessment, "claim_kind", None) != ClaimKind.CALL_EDGE:
            continue
        _target = getattr(_assessment, "claim_target", None)
        _eid = getattr(_assessment, "evidence_id", None)
        if _target is None or _eid is None or _eid not in evidence_by_id:
            continue
        _caller = str(getattr(_target, "counterpart", "") or "").strip()
        _callee = str(getattr(_target, "symbol", "") or "").strip()
        if not _caller or not _callee:
            continue
        chain.append(
            CausalRelationship(
                cause=_caller,
                effect=f"{_callee} invoked",
                mechanism="calls",
                confidence=0.7,
                evidence_ids=[_eid],
            )
        )
    return chain


async def _gate_conclusion_or_fail(
    tenant_id: str,
    investigation_id: UUID,
    ctx: Any,
    shadow_notes: list[dict[str, Any]] | None = None,
    triage_route: str = "",
) -> Any:
    """Evaluate ConclusionGate for a requested COMPLETED conclusion (F-051).

    Returns ``InvestigationStatus.COMPLETED`` only when the gate approves;
    otherwise returns ``InvestigationStatus.FAILED`` so the denial and its
    blockers are persisted in the lifecycle rather than swallowed. On
    approval, also persists the Finding + InvestigationConclusion that
    justify COMPLETED (Part 11.2) — the conclusion read path and clustering
    (Part 11.7+) depend on this corpus actually existing.
    """
    from investigation_agent_platform.application.investigation.verification import (
        CLAIM_BINDING_CBV1,
        CLAIM_BINDING_CBV2,
        CLAIM_BINDING_CBV3,
        VERIFIER_VERSION_CURRENT,
        ClaimBindingEnforcement,
        ConclusionGate,
        RootCauseVerificationPolicy,
        UnknownVerifierVersion,
        VerificationEngine,
        VerificationStatus,
        VerifierVersionUnavailable,
        bound_kept_evidence_only,
        resolve_claim_binding_version,
    )
    from investigation_agent_platform.domain.finding.models import (
        ConclusionStatus,
        Finding,
        FindingType,
        InvestigationConclusion,
        classify_terminal_handoff,
    )
    from investigation_agent_platform.domain.hypothesis.models import ClaimKind
    from investigation_agent_platform.domain.investigation.models import InvestigationStatus

    evidence_repo: Any = getattr(ctx, "evidence_repo", None)
    hypothesis_repo: Any = getattr(ctx, "hypothesis_repo", None)
    finding_repo: Any = getattr(ctx, "finding_repo", None)
    evidence_items: list[Any] = []
    hypotheses: list[Any] = []
    if evidence_repo is not None:
        evidence_items = await evidence_repo.find_by_investigation_id(tenant_id, investigation_id)
    if hypothesis_repo is not None:
        hypotheses = await hypothesis_repo.find_by_investigation_id(tenant_id, investigation_id)
    if not hypotheses:
        story_log(
            logger,
            logging.WARNING,
            "ConclusionGate: no hypotheses; denying COMPLETED",
            comp=COMP_ACTVTY,
            func="_gate_conclusion_or_fail",
            stage="conclude",
            step="gate",
            outcome=OUTCOME_SKIPPED,
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            event="ConclusionGate denied COMPLETED",
            outputs={"reason": "no_hypotheses"},
            reason="no_hypotheses",
        )
        return InvestigationStatus.FAILED

    from investigation_agent_platform.application.investigation.verification import (
        CausalRelationship,
        rank_hypotheses,
        select_verifiable_candidate,
    )

    contradiction_repo: Any = getattr(ctx, "contradiction_repo", None)
    contradictions: list[Any] = []
    if contradiction_repo is not None:
        try:
            contradictions = await contradiction_repo.find_by_investigation_id(
                tenant_id, investigation_id
            )
        except Exception:
            contradictions = []
    evidence_by_id = {getattr(e, "evidence_id", None): e for e in evidence_items}

    def _resolve(ids: list[Any]) -> list[Any]:
        out = []
        for raw in ids or []:
            try:
                key = raw if not isinstance(raw, str) else UUID(raw)
            except (ValueError, TypeError, AttributeError):
                continue
            item = evidence_by_id.get(key)
            if item is not None:
                out.append(item)
        return out

    # Phase 1 (claim binding) linkage state, initialized before any
    # abstention path can read it (early returns call _persist_abstention
    # before the timeline read below runs).
    _conclude_recorded: list[Any] | None = None
    _conclude_enforcement = None

    async def _persist_abstention(
        kind: str, limitations: list[str], hypothesis_id: Any = None
    ) -> None:
        """Honest abstention is a successful terminal outcome (P0-3): persist
        an INSUFFICIENT_EVIDENCE / NO_ROOT_CAUSE_FOUND conclusion with its
        limitations instead of a generic FAILED."""
        if finding_repo is None:
            return
        _proven_callers, _unresolved_callers, _caller_coverage = (
            VerificationEngine.caller_edge_diagnostics(candidate)
            if VerificationEngine.resolve_claim_kind(
                candidate,
                strict=claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3),
            )
            == ClaimKind.CALL_EDGE
            else ([], [], None)
        )
        # Phase 1 (claim binding): an abstention record must never present
        # an unretrieved caller as proven. When linkage was observed,
        # proven callers are restricted to claim-local kept edges; callers
        # whose only evidence is bank-fallback stay unresolved. Unobserved
        # linkage keeps the Step 5 diagnostics byte-identically.
        if (
            claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3)
            and _conclude_enforcement is not None
            and _conclude_enforcement.linkage_observed
            and _proven_callers
        ):
            try:
                _kept_ids = set(_conclude_enforcement.kept_ids or [])
                _linked_ids: set[str] = set()
                for _banked, _att in _conclude_recorded or []:
                    for _raw in _banked or []:
                        _s = str(_raw or "").strip()
                        if _s:
                            _linked_ids.add(_s)
                _local_ids = _kept_ids & _linked_ids
                _caller_evidence: dict[str, str] = {}
                for _asm in getattr(candidate, "assessments", None) or []:
                    try:
                        if getattr(_asm, "claim_kind", None) != ClaimKind.CALL_EDGE:
                            continue
                        _t = getattr(_asm, "claim_target", None)
                        _c = str(getattr(_t, "counterpart", "") or "").strip()
                        _e = str(getattr(_asm, "evidence_id", "") or "")
                        if _c and _e and _c not in _caller_evidence:
                            _caller_evidence[_c] = _e
                    except Exception:
                        continue
                _proven_local = sorted(
                    [c for c in _proven_callers if _caller_evidence.get(c) in _local_ids],
                    key=str.casefold,
                )
                _demoted_callers = sorted(
                    {c for c in _proven_callers if c not in set(_proven_local)},
                    key=str.casefold,
                )
                _proven_callers = _proven_local
                if _demoted_callers:
                    _unresolved_callers = sorted(
                        set(_unresolved_callers) | set(_demoted_callers),
                        key=str.casefold,
                    )
            except Exception:
                pass
        status = (
            ConclusionStatus.NO_ROOT_CAUSE_FOUND
            if kind == "refuted"
            else ConclusionStatus.INSUFFICIENT_EVIDENCE
        )
        _ranked_alternatives = _ranked_alternative_statements()
        _abstention_next = VerificationEngine.next_check_for_abstention(
            kind, list(limitations or [])
        )
        # P4: refusal/abstention still records what was ruled out. The
        # elimination record resolves sibling refuting IDs against the bank
        # (fail-closed); without a bank it stays empty, never fabricated.
        _abstention_eliminated: list = []
        try:
            _abstention_eliminated = VerificationEngine.elimination_record(
                candidate, list(hypotheses or []), list(evidence_by_id.values())
            )
        except Exception:
            _abstention_eliminated = []
        # P4: the handoff is explicit. Abstention/refusal conclusions carry
        # the S7 handoff in recommended_next_steps with target "n/a"; the
        # helper never mutates the investigation status target.
        _abstention_handoff = classify_terminal_handoff(
            status=status,
            reason="refuted" if kind == "refuted" else "insufficient",
            next_check=_abstention_next,
            limitations=[str(l) for l in (limitations or [])],
            ranked_alternatives=_ranked_alternatives,
            eliminated_rivals=list(_abstention_eliminated or []),
        )
        await finding_repo.save_conclusion(
            tenant_id,
            InvestigationConclusion(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                status=status,
                root_cause=None,
                supporting_evidence_ids=[],
                supporting_hypothesis_ids=([hypothesis_id] if hypothesis_id is not None else []),
                contradicting_evidence_ids=[],
                confidence=0.0,
                limitations=[str(l)[:500] for l in limitations][:20],
                # F: abstention still publishes ranked alternatives so
                # partial answers survive instead of evaporating.
                ranked_alternatives=_ranked_alternatives,
                next_check=_abstention_next,
                recommended_next_steps=list(_abstention_handoff.recommended_next_steps or []),
                eliminated_rivals=list(_abstention_eliminated or []),
                handoff=_abstention_handoff,
                proven_callers=_proven_callers,
                unresolved_callers=_unresolved_callers,
                caller_coverage=_caller_coverage,
                verifier_version=verifier_version,
            ),
        )
        # Debug story: abstention terminal. The honest-INSUFFICIENT_EVIDENCE
        # path is the most common story ending — log it like a conclusion.
        _story_debug(
            tenant_id=str(tenant_id),
            investigation_id=str(investigation_id),
            event="story.conclude_decision",
            status=str(status.value if hasattr(status, "value") else status),
            root_cause=None,
            supporting_evidence_ids=[],
            limitations=[str(l)[:500] for l in (limitations or [])][:20],
            proven_callers=list(_proven_callers or []),
            unresolved_callers=list(_unresolved_callers or []),
            caller_coverage=_caller_coverage,
            confidence=0.0,
        )
        # Phase 4.2: abstention volume with limitations (closed kind).
        try:
            _emit_telemetry(
                ctx, tenant_id, investigation_id, "record_conclusion_limitation", "abstention"
            )
        except Exception:
            pass

    def _ranked_alternative_statements() -> list[str]:
        try:
            ranked_hyps = rank_hypotheses(list(hypotheses or []))
            out: list[str] = []
            for _hyp in ranked_hyps:
                try:
                    _status = str(
                        getattr(getattr(_hyp, "status", ""), "value", getattr(_hyp, "status", ""))
                    )
                    if _status in ("REJECTED", "CONTRADICTED"):
                        continue
                    _statement = str(getattr(_hyp, "statement", "") or "").strip()
                    if not _statement:
                        continue
                    out.append(f"{_status}: {_statement}"[:500])
                    if len(out) >= 3:
                        break
                except Exception:
                    continue
            return out
        except Exception:
            return []

    policy = RootCauseVerificationPolicy()

    verifier_version = VERIFIER_VERSION_CURRENT
    _version_source = "unified-dev"
    _inv = None
    # Phase 1 (claim binding): resolve the recorded pin on the same
    # metadata contract as verify. Unknown/unreadable pins fail closed
    # (deny COMPLETED); unmarked resolves to current (cbv1 in rollout).
    claim_binding_version = CLAIM_BINDING_CBV1
    _binding_source = "unmarked"
    _cb_repo = getattr(ctx, "investigation_repo", None)
    if _cb_repo is not None:
        try:
            _cb_inv = await _cb_repo.get_by_id(tenant_id, investigation_id)
        except Exception as exc:
            await _persist_abstention(
                "refuted",
                [
                    f"claim binding version unavailable: repository read failed ({type(exc).__name__})"
                ],
            )
            return InvestigationStatus.FAILED
        if _cb_inv is None:
            await _persist_abstention(
                "refuted", ["claim binding version unavailable: investigation not found"]
            )
            return InvestigationStatus.FAILED
        try:
            claim_binding_version, _binding_source = resolve_claim_binding_version(
                getattr(_cb_inv, "metadata", None)
            )
        except (UnknownVerifierVersion, VerifierVersionUnavailable) as exc:
            await _persist_abstention("refuted", [f"claim binding version unavailable: {exc}"])
            return InvestigationStatus.FAILED
        _inv = _cb_inv
    engine = VerificationEngine(
        verifier_version=verifier_version,
        claim_binding_version=claim_binding_version,
    )
    ranked = rank_hypotheses(list(hypotheses))
    # G9: same resolvable-citation preference as the verify activity.
    candidate = select_verifiable_candidate(ranked, evidence_by_id)
    if candidate is None:
        await _persist_abstention("refuted", ["all hypotheses rejected or contradicted"])
        return InvestigationStatus.COMPLETED
    bound = _resolve(getattr(candidate, "supporting_evidence_ids", []) or [])
    refuting = _resolve(getattr(candidate, "refuting_evidence_ids", []) or [])
    # Phase 3 (claim binding): fast-path repair records collected by the
    # location/history wraps below. Initialized up front so the waiver and
    # enforcement blocks never read an unbound name.
    _conclude_repairs: list[Any] = []
    # Phase 1 (claim binding): timeline-backed linkage for the conclude
    # working set. Fail-open to None (unmapped degradation); a read fault
    # never blocks the conclusion path. (Initialized to None above so early
    # abstention returns stay safe.)
    if claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3):
        try:
            from investigation_agent_platform.application.evidence.retrieval_ledger import (
                recorded_retrieval_attempts as _conclude_timeline_attempts,
            )

            _conclude_tl_repo = getattr(ctx, "timeline_repo", None)
            if _conclude_tl_repo is not None:
                try:
                    _conclude_events = await _conclude_tl_repo.find_by_investigation_id(
                        tenant_id, investigation_id
                    )
                except Exception:
                    _conclude_events = None
                if _conclude_events is not None:
                    _conclude_recorded = _conclude_timeline_attempts(_conclude_events or [])
        except Exception:
            _conclude_recorded = None
    # Tranche B: same statement-matched range-read inclusion as verify.
    # Under cbv2 the inclusion is gated: only claim-local-linked or
    # explicitly cited rows join; it can never substitute for zero
    # cited-kept survival.
    try:
        _bound_ids = {getattr(e, "evidence_id", None) for e in bound}
        if claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3):
            _c_cited: set[str] = set()
            try:
                for _raw in getattr(candidate, "supporting_evidence_ids", []) or []:
                    _c_cited.add(str(_raw))
                for _asm in getattr(candidate, "assessments", []) or []:
                    _c_cited.add(str(getattr(_asm, "evidence_id", "") or ""))
                for _step in getattr(candidate, "causal_steps", []) or []:
                    for _raw in getattr(_step, "evidence_ids", []) or []:
                        _c_cited.add(str(_raw))
            except Exception:
                pass
            _c_linked: set[str] = set()
            try:
                for _banked, _att in _conclude_recorded or []:
                    for _raw in _banked or []:
                        _c_linked.add(str(_raw))
            except Exception:
                pass
            _c_allow = _c_cited | _c_linked
            for _extra in VerificationEngine.statement_range_reads(
                str(getattr(candidate, "statement", "") or ""),
                list(evidence_by_id.values()),
            ):
                _c_xid = getattr(_extra, "evidence_id", None)
                if _c_xid in _bound_ids:
                    continue
                if str(_c_xid) not in _c_allow:
                    continue
                _bound_ids.add(_c_xid)
                bound.append(_extra)
        else:
            for _extra in VerificationEngine.statement_range_reads(
                str(getattr(candidate, "statement", "") or ""),
                list(evidence_by_id.values()),
            ):
                if getattr(_extra, "evidence_id", None) not in _bound_ids:
                    _bound_ids.add(getattr(_extra, "evidence_id", None))
                    bound.append(_extra)
    except Exception:
        pass
    # Phase 1 (claim binding): filter the conclude working set to kept
    # citations before any validate_* fast path runs. Under cbv1 this
    # block is skipped byte-identically.
    if claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3):
        try:
            _conclude_enforcement = engine.claim_binding_enforcement(
                candidate,
                list(evidence_by_id.values()),
                _conclude_recorded,
            )
            if _conclude_enforcement is not None:
                _c_kept = set(_conclude_enforcement.kept_ids or [])
                _c_cited = set(_conclude_enforcement.cited_ids or [])
                bound, _c_evap, _c_ckept = bound_kept_evidence_only(bound, _c_kept, _c_cited)
        except Exception:
            # Fail closed: without a partition the filtered set is
            # untrusted; a fault enforcement forces abstention via the
            # waiver block instead of concluding on it.
            _conclude_enforcement = ClaimBindingEnforcement(fault=True)
    # D3 Phase 0 shadow tap T3 (read-only; never feeds back). Mirrors T1
    # on conclude-time loaded rows; observations join the conclude gate
    # outcome as CONCLUDE_GATE. Same isolation: any fault records a
    # SHADOW_FAILED marker; the native path continues unchanged.
    _conclude_shadow_notes: list[dict[str, Any]] = shadow_notes if shadow_notes is not None else []
    try:
        _conclude_shadow_mode = resolve_definition_mode()
    except ShadowError as exc:
        story_log(
            logger,
            logging.ERROR,
            "conclude gate refusing governed path",
            comp=COMP_ACTVTY,
            func="_gate_conclusion_or_fail",
            stage="conclude",
            step="gate",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            event="conclude gate refusing governed path",
            error=str(exc),
        )
        raise PlatformConfigurationError(f"definition shadow refused: {exc}") from exc
    if _conclude_shadow_mode == "shadow":
        try:
            try:
                _shadow_info_c = activity.info()
                _shadow_identity_c = {
                    "namespace": _shadow_info_c.namespace,
                    "workflow_id": _shadow_info_c.workflow_id,
                    "workflow_run_id": _shadow_info_c.workflow_run_id,
                    "activity_id": _shadow_info_c.activity_id,
                    "attempt": _shadow_info_c.attempt,
                }
            except Exception:
                _shadow_identity_c = {}
            _shadow_table_c, _shadow_registry_c = _load_definition_dispatch_table()
            _shadow_obs_c = run_shadow_turn(
                candidate=candidate,
                bound=list(bound),
                refuting_ids=[str(getattr(e, "evidence_id", "") or "") for e in (refuting or [])],
                triage_route=str(triage_route or ""),
                verifier_version=str(verifier_version),
                investigation_id=str(investigation_id),
                hypothesis_id=str(getattr(candidate, "id", "") or ""),
                application_id=str(getattr(_inv, "application_id", "") or ""),
                input_set="CONCLUDE_GATE",
                table=_shadow_table_c,
                registry=_shadow_registry_c,
            )
            if _shadow_obs_c is not None:
                _shadow_obs_c["activity_identity"] = _shadow_identity_c
                _conclude_shadow_notes.append(_shadow_obs_c)
        except Exception:
            _conclude_shadow_notes.append(
                {"capture_error": True, "reason": "tap-fault", "input_set": "CONCLUDE_GATE"}
            )

    def _conclude_observe(note: dict[str, Any]) -> None:
        """T3 mirror-note sink: retags engine notes as CONCLUDE_GATE.

        Mode-gated like the verify-side sink: shadow-off records
        nothing, so the conclude result carries no additive shadow
        key (D3P-N21/F09).
        """
        try:
            if _conclude_shadow_mode != "shadow":
                return
            if isinstance(note, dict):
                tagged = dict(note)
                tagged["tap"] = "T3"
                tagged["input_set"] = "CONCLUDE_GATE"
                _conclude_shadow_notes.append(tagged)
        except Exception:
            pass

    def _resolve_evidence_key(raw: Any) -> Any:
        if raw in evidence_by_id:
            return raw
        try:
            as_uuid = raw if not isinstance(raw, str) else UUID(raw)
            if as_uuid in evidence_by_id:
                return as_uuid
            as_str = str(raw)
            if as_str in evidence_by_id:
                return as_str
        except (ValueError, TypeError, AttributeError):
            pass
        return None

    causal_chain = []
    for s in getattr(candidate, "causal_steps", []) or []:
        resolved_ids = []
        for raw_eid in s.evidence_ids or []:
            matched_key = _resolve_evidence_key(raw_eid)
            if matched_key is not None:
                resolved_ids.append(matched_key)
        if resolved_ids:
            causal_chain.append(
                CausalRelationship(
                    cause=s.cause,
                    effect=s.effect,
                    mechanism=s.mechanism,
                    confidence=0.7,
                    evidence_ids=resolved_ids,
                )
            )
    causal_chain = [c for c in causal_chain if c.evidence_ids]
    if not causal_chain and (
        verifier_version == VERIFIER_VERSION_CURRENT
        and VerificationEngine.resolve_claim_kind(
            candidate,
            strict=claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3),
        )
        == ClaimKind.CALL_EDGE
    ):
        causal_chain = _caller_edge_causal_chain(candidate, evidence_by_id)
    # Finding L: opinion-only candidates skip every conclude validator.
    # The shared predicate (N) screens on assessments alone — no evidence
    # reads beyond the already-resolved working set, no model calls. The
    # abstention reasons mirror the full path's reason block for an
    # all-NC candidate (no validator could answer it by kind-gate), with
    # the machine-readable NOT_CHECKABLE_SKIPPED marker first — the same
    # evidence-only channel the conclude stage would have used.
    try:
        from investigation_agent_platform.application.evidence.retrieval_ledger import (
            all_typed_claims_not_checkable as _all_nc_conclude,
        )
    except Exception:
        _all_nc_conclude = None
    try:
        _nc_early_conclude = (
            bool(_all_nc_conclude(candidate)) if _all_nc_conclude is not None else False
        )
    except Exception:
        _nc_early_conclude = False
    if _nc_early_conclude:
        _nc_reasons = ["NOT_CHECKABLE_SKIPPED"]
        try:
            if not bound:
                _nc_reasons.append("no bound supporting evidence")
        except Exception:
            pass
        try:
            if refuting:
                _nc_reasons.append(f"{len(refuting)} refuting evidence item(s) bound")
        except Exception:
            pass
        try:
            if not causal_chain:
                _nc_reasons.append("no validated causal chain")
        except Exception:
            pass
        await _persist_abstention(
            "refuted" if refuting else "insufficient", _nc_reasons, candidate.id
        )
        return InvestigationStatus.COMPLETED
    # G1: location-answer fast path (mirrors verify_root_cause_activity).
    _verify_engine = VerificationEngine(
        verifier_version=verifier_version,
        claim_binding_version=claim_binding_version,
    )
    code_value_verification = _verify_engine.validate_code_value_claim(
        candidate, bound, contradictions
    )
    code_value_claim = (
        VerificationEngine.claims_field_value(str(getattr(candidate, "statement", "") or ""))
        or str(VerificationEngine._fact_category_of(candidate) or "") == "SOURCE_VALUE_FACT"
    )
    code_value_answered = code_value_verification is not None and not refuting
    # Phase 3 (claim binding): fast-path repair wraps (D6 sites 3/5).
    # revalidate_with_repair runs each validate call verbatim —
    # identical under cbv1 — and performs one repair pass under
    # cbv2/cbv3. Repairs collect here for the waiver amendment,
    # enforcement extension, and diagnostics attachment below.
    try:
        history_verification, _c_history_repairs = _verify_engine.revalidate_with_repair(
            lambda h, w: _verify_engine.validate_history_claim(h, w, contradictions),
            candidate,
            bound,
            list(evidence_by_id.values()),
            contradictions,
            None,
            enforcement=_conclude_enforcement,
            expect_kinds=(ClaimKind.HISTORY,),
        )
        _conclude_repairs.extend(list(_c_history_repairs or []))
    except Exception:
        history_verification = _verify_engine.validate_history_claim(
            candidate, bound, contradictions
        )
    history_answered = history_verification is not None and not refuting
    if code_value_claim and not code_value_answered:
        await _persist_abstention(
            "refuted" if refuting else "insufficient",
            ["code value claim lacks a matching deterministic resolution"],
            candidate.id,
        )
        return InvestigationStatus.COMPLETED
    try:
        location_verification, _c_location_repairs = _verify_engine.revalidate_with_repair(
            lambda h, w: _verify_engine.validate_location_claim(
                h, w, contradictions, _conclude_observe
            ),
            candidate,
            bound,
            list(evidence_by_id.values()),
            contradictions,
            None,
            enforcement=_conclude_enforcement,
            expect_kinds=(ClaimKind.DEFINITION, ClaimKind.CALL_EDGE),
        )
        _conclude_repairs.extend(list(_c_location_repairs or []))
    except Exception:
        location_verification = _verify_engine.validate_location_claim(
            candidate, bound, contradictions, _conclude_observe
        )
    location_answered = (
        location_verification is not None
        and not refuting
        and not code_value_claim
        and not history_answered
        and not (
            verifier_version == VERIFIER_VERSION_CURRENT
            and VerificationEngine.resolve_claim_kind(
                candidate,
                strict=claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3),
            )
            == ClaimKind.CALL_EDGE
        )
    )
    caller_edge_validated = False
    if (
        verifier_version == VERIFIER_VERSION_CURRENT
        and VerificationEngine.resolve_claim_kind(
            candidate,
            strict=claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3),
        )
        == ClaimKind.CALL_EDGE
    ):
        caller_edge_gate = _verify_engine.evaluate_caller_edge(candidate, bound)
        caller_edge_validated = (
            bool(caller_edge_gate is not None and caller_edge_gate[0]) and not refuting
        )
    # T1-1: doc validation up front so the abstention check below sees it.
    doc_answered = False
    doc_verification = None
    try:
        doc_verification = VerificationEngine(
            verifier_version=verifier_version,
            claim_binding_version=claim_binding_version,
        ).validate_doc_claim(candidate, bound, contradictions)
        doc_answered = doc_verification is not None and not refuting
    except Exception:
        pass
    # #65: state validation up front, same abstention visibility.
    state_answered = False
    state_verification = None
    if (
        not location_answered
        and not code_value_answered
        and not history_answered
        and not doc_answered
        and not code_value_claim
    ):
        try:
            state_verification = VerificationEngine(
                verifier_version=verifier_version,
                claim_binding_version=claim_binding_version,
            ).validate_state_claim(candidate, bound, contradictions)
            state_answered = state_verification is not None and not refuting
        except Exception:
            pass
    # Phase 1 (claim binding): zero cited-kept survival forces the
    # abstention path even when *_validated flags would otherwise waive
    # the causal-chain requirement (D6 gate-waiver block). Under cbv1
    # this is always False: no behavior change.
    # Phase 3 (claim binding, D6 site 4): applied rebinds extend the
    # enforcement before the waiver so a rebound donor stands in for
    # its citation in the kept count; the waiver amendment is pinned
    # by a conclude-publishes-with-repair test plus a
    # shadow-still-abstains test.
    if (
        claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3)
        and _conclude_enforcement is not None
        and _conclude_repairs
    ):
        try:
            _c_applied_donors = [
                str(getattr(r, "donor_id", "") or "")
                for r in _conclude_repairs
                if str(getattr(r, "outcome", "") or "") == "applied"
                and str(getattr(r, "donor_id", "") or "")
            ]
            if _c_applied_donors:
                _conclude_enforcement = VerificationEngine.enforcement_with_repairs(
                    _conclude_enforcement, _c_applied_donors
                )
        except Exception:
            pass
    _conclude_binding_blocks = False
    if (
        claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3)
        and _conclude_enforcement is not None
    ):
        try:
            _cb_cited = list(_conclude_enforcement.cited_ids or [])
            # Zero cited-kept survival always blocks, as does a binding
            # fault (fail closed, never conclude on an unpartitioned set).
            # Unmapped-only verification (observed linkage, nothing
            # claim-local) blocks the causal-chain waiver only: with a
            # causal chain present, the gate does not need the waiver and
            # the UNMAPPED limitation rides the published conclusion instead.
            # enforcement_with_repairs folds applied rebinds into
            # cited_kept_count, so a rebound citation counts as kept.
            if _conclude_enforcement.fault or (
                _cb_cited
                and (
                    _conclude_enforcement.cited_kept_count == 0
                    or (
                        _conclude_enforcement.unmapped
                        and not _conclude_enforcement.has_claim_local
                        and _conclude_enforcement.linkage_observed
                        and not causal_chain
                    )
                )
            ):
                _conclude_binding_blocks = True
        except Exception:
            pass
    if (
        not bound
        or refuting
        or _conclude_binding_blocks
        or (
            not causal_chain
            and not location_answered
            and not code_value_answered
            and not history_answered
            and not doc_answered
            and not state_answered
            and not caller_edge_validated
        )
    ):
        reasons = []
        if not bound:
            reasons.append("no bound supporting evidence")
        if _conclude_binding_blocks:
            reasons.append(
                "claim citations kept zero bound rows (evaporated); "
                "INCONCLUSIVE pending smallest-fetch re-retrieval"
            )
        if refuting:
            reasons.append(f"{len(refuting)} refuting evidence item(s) bound")
        if (
            not causal_chain
            and not location_answered
            and not code_value_answered
            and not history_answered
            and not doc_answered
            and not state_answered
            and not caller_edge_validated
        ):
            reasons.append("no validated causal chain")
        if code_value_claim and not code_value_answered:
            reasons.append("code value claim lacks a matching deterministic resolution")
        # Phase 4 (claim binding): opinion-only abstention carries its
        # machine-readable marker. Candidate-scoped (assessments are
        # hydrated here; the marker is not). Finding N: the all-typed-NC
        # check is the shared predicate, not an inline re-implementation.
        try:
            from investigation_agent_platform.application.evidence.retrieval_ledger import (
                all_typed_claims_not_checkable as _all_nc,
            )
        except Exception:
            _all_nc = None
        try:
            _opinion_only = bool(_all_nc(candidate)) if _all_nc is not None else False
        except Exception:
            _opinion_only = False
        if _opinion_only:
            reasons.append("NOT_CHECKABLE_SKIPPED")
        await _persist_abstention("refuted" if refuting else "insufficient", reasons, candidate.id)
        return InvestigationStatus.COMPLETED
    if caller_edge_validated or location_answered:
        verification = location_verification
    elif code_value_answered:
        verification = code_value_verification
    elif history_answered:
        verification = history_verification
    elif doc_answered and doc_verification is not None:
        verification = doc_verification
    elif state_answered and state_verification is not None:
        verification = state_verification
    # Phase 1 (claim binding): route the selected fast-path verification
    # through authoritative enforcement (fast paths bypass the engine's
    # internal enforcement). Flips the answered flags on an enforced
    # INCONCLUSIVE so the general branch re-evaluates consistently.
    # Under cbv1 _conclude_enforcement is None: no-op.
    try:
        _enforce_target = verification
    except NameError:
        # No fast path taken: nothing selected to enforce; the general
        # branch below evaluates (and enforces) from scratch.
        _enforce_target = None
    # Phase 3 (claim binding, D6 site 5): the enforcement was already
    # extended with applied rebinds before the waiver block, so the
    # choke below keeps donor corroborating. Repairs attach to the
    # taken verification's diagnostics afterwards.
    if (
        claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3)
        and _conclude_enforcement is not None
        and _enforce_target is not None
        and (
            caller_edge_validated
            or location_answered
            or code_value_answered
            or history_answered
            or doc_answered
            or state_answered
        )
    ):
        try:
            verification = VerificationEngine.attach_repairs(
                engine.enforce_claim_binding(candidate, _enforce_target, _conclude_enforcement),
                _conclude_repairs,
            )
            try:
                _enf_status = str(
                    getattr(
                        getattr(verification, "status", ""),
                        "value",
                        getattr(verification, "status", ""),
                    )
                    or ""
                )
            except Exception:
                _enf_status = ""
                if _enf_status != VerificationStatus.VERIFIED.value:
                    caller_edge_validated = False
                    location_answered = False
                    code_value_answered = False
                    history_answered = False
                    doc_answered = False
                    state_answered = False
        except Exception:
            # Fail closed (NEW-1): an enforcement fault must never leave a
            # taken fast-path VERIFIED standing. Force the abstention path
            # below instead of concluding on an unenforced verdict.
            _conclude_binding_blocks = True
            try:
                verification = engine._fault_inconclusive(candidate)
            except Exception:
                pass
    if (
        not location_answered
        and not code_value_answered
        and not history_answered
        and not doc_answered
        and not state_answered
        and not caller_edge_validated
        and not code_value_claim
    ):
        _gate_candidate_id = getattr(candidate, "id", None)
        _conclude_kwargs: dict[str, Any] = {}
        # Phase 1 (claim binding): timeline-backed linkage for the
        # general-branch enforcement; marker-less legacy keeps the exact
        # legacy call shape.
        if claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3) and _conclude_recorded:
            _conclude_kwargs["recorded_attempts"] = _conclude_recorded
        # Phase 3 (claim binding): the full bank feeds the repair donor
        # pool (the working set may exclude uncited donors). cbv2/cbv3
        # runs only — legacy keeps the exact call shape.
        if claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3):
            try:
                _conclude_kwargs["evidence_bank"] = list(evidence_by_id.values())
            except Exception:
                pass
        # Engine-branch repairs are already attached in-engine; the
        # outer attach only adds fast-path-wrap repairs (dedupes).
        verification = VerificationEngine.attach_repairs(
            engine.evaluate_hypothesis(
                candidate,
                bound,
                contradictions,
                causal_chain,
                policy,
                sibling_hypotheses=[
                    h for h in (hypotheses or []) if getattr(h, "id", None) != _gate_candidate_id
                ],
                observer=_conclude_observe,
                **_conclude_kwargs,
            ),
            _conclude_repairs,
        )
    gate = ConclusionGate()
    decision = await gate.evaluate(
        tenant_id,
        investigation_id,
        verification,
        contradictions,
        causal_chain,
        bound,
        policy,
        location_validated=location_answered,
        code_value_validated=code_value_answered,
        history_validated=history_answered,
        doc_validated=doc_answered,
        state_validated=state_answered,
        caller_edges_validated=caller_edge_validated,
    )
    if not decision.approved:
        story_log(
            logger,
            logging.WARNING,
            "ConclusionGate denied COMPLETED: %s",
            "; ".join(decision.blockers),
            comp=COMP_ACTVTY,
            func="_gate_conclusion_or_fail",
            stage="conclude",
            step="gate",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            event="ConclusionGate denied COMPLETED",
            outputs={"approved": False, "blockers": list(decision.blockers)},
            blockers=list(decision.blockers),
        )
        # Phase 2b: lead with the verification explanation so the persisted
        # next_check names the failed proof (range-read rebind, runtime
        # observation, source-value resolution) instead of the generic
        # chain message. Fail-open: explanation add-on never blocks.
        _denial_reasons: list[str] = []
        try:
            _expl = str(getattr(verification, "explanation", "") or "").strip()
            if _expl:
                _denial_reasons.append(_expl[:500])
        except Exception:
            pass
        _denial_reasons.extend(list(decision.blockers))
        await _persist_abstention("insufficient", _denial_reasons, candidate.id)
        return InvestigationStatus.COMPLETED

    # P0-3: persist the Finding + InvestigationConclusion that back this
    # approval. Atomic correctness boundary with COMPLETED: a persistence
    # failure raises (the investigation retries/fails loudly) instead of
    # publishing COMPLETED with conclusion=null. Citations are scoped to
    # the verified hypothesis's bound evidence — never the whole corpus —
    # and the causal chain is the validated relationship evidence, never
    # an arbitrary first ID.
    if finding_repo is None:
        raise ApplicationFailure(
            "Finding repository is not configured; cannot publish conclusion",
            type="CONCLUSION_STORE_UNAVAILABLE",
            non_retryable=True,
        )
    from investigation_agent_platform.domain.hypothesis.models import (
        HypothesisStatus as _HStatus,
    )

    hyp = candidate
    _proven_callers, _unresolved_callers, _caller_coverage = (
        VerificationEngine.caller_edge_diagnostics(hyp)
        if VerificationEngine.resolve_claim_kind(
            hyp,
            strict=claim_binding_version in (CLAIM_BINDING_CBV2, CLAIM_BINDING_CBV3),
        )
        == ClaimKind.CALL_EDGE
        else ([], [], None)
    )
    supporting_ids = [e.evidence_id for e in bound]
    causal_ids = [eid for c in causal_chain for eid in c.evidence_ids]
    if hyp.status == _HStatus.SUPPORTED and hypothesis_repo is not None:
        hyp = hyp.transition_status(_HStatus.VERIFIED)
        await hypothesis_repo.save(tenant_id, hyp, investigation_id, hyp.version)
    finding = Finding(
        tenant_id=tenant_id,
        investigation_id=investigation_id,
        finding_type=FindingType.OBSERVATION
        if (
            location_answered
            or code_value_answered
            or history_answered
            or doc_answered
            or state_answered
        )
        else FindingType.ROOT_CAUSE,
        title=hyp.title,
        statement=hyp.statement,
        evidence_ids=supporting_ids,
        related_hypothesis_ids=[hyp.id],
        causal_chain=causal_ids,
        confidence=verification.confidence.overall_confidence,
    )
    await finding_repo.save_finding_record(tenant_id, finding)
    # G-4: incident fusion. When a journey spine exists and two or more
    # OBSERVATION findings were persisted across steps, merge them into one
    # incident-level finding (union evidence, concatenated statements).
    # Fail-open: fusion never blocks the conclusion.
    try:
        await _fuse_journey_findings(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            finding_repo=finding_repo,
            evidence_repo=getattr(ctx, "evidence_repo", None),
        )
    except Exception as exc:
        story_log(
            logger,
            logging.WARNING,
            "journey fusion failed; single findings stand",
            comp=COMP_ACTVTY,
            func="_gate_conclusion_or_fail",
            stage="conclude",
            step="fuse",
            outcome=OUTCOME_SKIPPED,
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            event="journey fusion failed",
            error=str(exc),
        )
    # F: ranked alternatives (runner-ups, capped) computed before the
    # conclusion so partial answers survive abstention.
    _alternatives: list[str] = []
    try:
        for _other in ranked or []:
            try:
                if _other is candidate:
                    continue
                _statement = str(getattr(_other, "statement", "") or "").strip()
                if not _statement:
                    continue
                _status = str(
                    getattr(getattr(_other, "status", ""), "value", getattr(_other, "status", ""))
                )
                if _status in ("REJECTED", "CONTRADICTED"):
                    continue
                _alternatives.append(f"{_status}: {_statement}"[:500])
                if len(_alternatives) >= 3:
                    break
            except Exception:
                continue
    except Exception:
        _alternatives = []
    # Phase 2.4: the published conclusion must map to its citations.
    # Material sentences that map to no cited claim or evidence row demote
    # the conclusion to likely (never silently established), and the typed
    # limitations plus the single next check persist on the conclusion.
    from investigation_agent_platform.application.investigation.verification import (
        LimitationCode as _LimitationCode,
    )
    from investigation_agent_platform.application.investigation.verification import (
        VerificationEngine as _VerificationEngine,
    )
    from investigation_agent_platform.application.investigation.verification import (
        VerificationLimitation as _VerificationLimitation,
    )

    _unmapped: list[str] = []
    try:
        _causal_texts = [
            f"{getattr(c, 'cause', '')} {getattr(c, 'mechanism', '')} {getattr(c, 'effect', '')}"
            for c in (causal_chain or [])
        ]
        _unmapped = _VerificationEngine.map_conclusion_sentences(
            str(getattr(hyp, "statement", "") or ""), list(bound or []), _causal_texts
        )
        verification = _VerificationEngine.apply_conclusion_mapping(verification, _unmapped)
    except Exception:
        _unmapped = []
    _untested_rivals: list[str] = []
    try:
        _untested_rivals = _VerificationEngine.untested_rival_statements(
            candidate, list(hypotheses or [])
        )
    except Exception:
        _untested_rivals = []
    # P3 (S5/S6 rival bar): the elimination record — ruled-out rivals with
    # their ruling-out evidence. A causal ESTABLISHED verdict requires a
    # non-empty record; without one the verdict demotes to LIKELY (same
    # discipline as RIVAL_UNTESTED: no named rival, no established verdict).
    _eliminated_rivals: list = []
    try:
        _eliminated_rivals = _VerificationEngine.elimination_record(
            candidate, list(hypotheses or []), list(evidence_by_id.values())
        )
    except Exception:
        _eliminated_rivals = []
    _rival_bar_failed = False
    try:
        _tier = str(getattr(verification, "tier", "established") or "established")
        _vstatus = str(
            getattr(
                getattr(verification, "status", ""), "value", getattr(verification, "status", "")
            )
        )
        _would_establish = (
            _vstatus == "VERIFIED"
            and _tier != "likely"
            # P3 rival bar scopes to causal conclusions only: every
            # fast-path answer shape (location, code-value, doc, history,
            # state, caller-edge) is excluded, not just the first three.
            # History/state/caller-edge answers are verifier-confirmed on
            # their own shape; they are never causal verdicts.
            and not (
                location_answered
                or code_value_answered
                or doc_answered
                or history_answered
                or state_answered
                or caller_edge_validated
            )
        )
        if _would_establish and not _eliminated_rivals:
            _rival_bar_failed = True
    except Exception:
        _rival_bar_failed = False
    try:
        _verification_limitations = [
            f"{getattr(getattr(lim, 'code', ''), 'value', getattr(lim, 'code', ''))}: {getattr(lim, 'detail', '')}"[
                :500
            ]
            for lim in (getattr(verification, "limitations", None) or [])
        ][:20]
    except Exception:
        _verification_limitations = []
    if _rival_bar_failed:
        try:
            _verification_limitations.append(
                "RIVAL_UNBEATEN: no ruled-out rival with ruling-out evidence "
                "recorded; causal verdict publishes as likely, never established."
            )
        except Exception:
            pass
    _next_check: str | None = None
    try:
        # P3 rival bar: the demotion must feed next-check construction,
        # which reads typed limitations — not the formatted strings.
        # Without this the demoted likely conclusion carries no follow-up
        # telling the investigator to test and eliminate a rival.
        _conclude_limitations = list(getattr(verification, "limitations", None) or [])
        if _rival_bar_failed:
            try:
                _conclude_limitations = list(_conclude_limitations) + [
                    _VerificationLimitation(
                        code=_LimitationCode.RIVAL_UNBEATEN,
                        detail=(
                            "no ruled-out rival with ruling-out evidence recorded; "
                            "causal verdict publishes as likely, never established."
                        ),
                    )
                ]
            except Exception:
                pass
        _next_check = _VerificationEngine.next_check_for(
            _conclude_limitations,
            _untested_rivals,
            _unmapped,
        )
    except Exception:
        _next_check = None
    # Phase 4.2: conclusion limitation kinds + location-exemption state.
    # Additive and fail-open; publication never waits on metrics.
    try:
        for _lim_text in _verification_limitations:
            _emit_telemetry(
                ctx,
                tenant_id,
                investigation_id,
                "record_conclusion_limitation",
                _limitation_kind(_lim_text),
            )
        if location_answered:
            _emit_telemetry(
                ctx,
                tenant_id,
                investigation_id,
                "record_location_answer",
                bool(_untested_rivals),
            )
    except Exception:
        pass
    _conclusion_status = (
        ConclusionStatus.ANSWERED
        if (location_answered or code_value_answered or doc_answered)
        else (
            ConclusionStatus.ROOT_CAUSE_LIKELY
            if getattr(verification, "tier", "established") == "likely" or _rival_bar_failed
            else (
                ConclusionStatus.ROOT_CAUSE_ESTABLISHED
                if verification.status == VerificationStatus.VERIFIED
                else ConclusionStatus.ROOT_CAUSE_LIKELY
            )
        )
    )
    # P4: every terminal verdict persists its structured handoff. PROVED
    # verdicts carry an empty follow-up; LIKELY verdicts fall through the
    # classifier so their next fetch survives instead of being dropped.
    _verdict_handoff = classify_terminal_handoff(
        status=_conclusion_status,
        next_check=_next_check,
        limitations=list(_verification_limitations or []),
        ranked_alternatives=list(_alternatives or []),
        eliminated_rivals=list(_eliminated_rivals or []),
    )
    conclusion = InvestigationConclusion(
        tenant_id=tenant_id,
        investigation_id=investigation_id,
        limitations=_verification_limitations,
        next_check=_next_check,
        status=_conclusion_status,
        root_cause=hyp.statement,
        supporting_evidence_ids=supporting_ids,
        supporting_hypothesis_ids=[hyp.id],
        contradicting_evidence_ids=[e.evidence_id for e in refuting],
        confidence=verification.confidence.overall_confidence,
        # D5: in-band conclusions publish best-reason/against-evidence
        # with the provisional marker; established stays clean.
        best_reason=str(getattr(verification, "best_reason", "") or "")[:2000],
        against_evidence=[
            str(item)[:300]
            for item in list(getattr(verification, "against_evidence", []) or [])[:20]
        ],
        provisional=getattr(verification, "tier", "established") == "likely" or _rival_bar_failed,
        ranked_alternatives=_alternatives,
        eliminated_rivals=list(_eliminated_rivals),
        handoff=_verdict_handoff,
        proven_callers=_proven_callers,
        unresolved_callers=_unresolved_callers,
        caller_coverage=_caller_coverage,
        verifier_version=verifier_version,
    )
    await finding_repo.save_conclusion(tenant_id, conclusion)
    # Debug story: final conclusion payload. Closes the run's story with
    # the terminal verdict and its support.
    _story_debug(
        tenant_id=tenant_id,
        investigation_id=investigation_id,
        event="story.conclude_decision",
        status=str(getattr(conclusion.status, "value", conclusion.status)),
        root_cause=str(getattr(conclusion, "root_cause", "") or "")[:500],
        supporting_evidence_ids=[str(e) for e in (supporting_ids or [])],
        limitations=list(getattr(conclusion, "limitations", []) or []),
        proven_callers=list(getattr(conclusion, "proven_callers", []) or []),
        unresolved_callers=list(getattr(conclusion, "unresolved_callers", []) or []),
        caller_coverage=getattr(conclusion, "caller_coverage", None),
        confidence=float(getattr(conclusion, "confidence", 0.0) or 0.0),
    )
    evidence_ids = supporting_ids
    # First producer for the evidence_relationships table: verified
    # hypothesis -> supporting evidence CORROBORATES edges, so future
    # CORRELATE actions traverse real rows instead of an empty graph.
    # Best-effort like the rows above — never reverts the approval.
    try:
        _inv_repo = getattr(ctx, "investigation_repo", None)
        _engine = getattr(ctx, "engine", None)
        if _inv_repo is not None and _engine is not None:
            _inv = await _inv_repo.get_by_id(tenant_id, investigation_id)
            if _inv is not None:
                from sqlalchemy.ext.asyncio import async_sessionmaker

                from investigation_agent_platform.infrastructure.correlation.repository import (
                    PostgresRelationshipRepository,
                )
                from investigation_agent_platform.ports.persistence.repositories import (
                    EvidenceRelationshipRecord,
                )

                _rel_repo = PostgresRelationshipRepository(
                    async_sessionmaker(_engine, expire_on_commit=False)
                )
                _rel_edges = [
                    EvidenceRelationshipRecord(
                        tenant_id=tenant_id,
                        application_id=_inv.application_id,
                        source_id=str(hyp.id),
                        target_id=str(eid),
                        type="CORROBORATES",
                        confidence=verification.confidence.overall_confidence,
                        metadata={"reason": hyp.statement[:500]},
                    )
                    for eid in evidence_ids
                ]
                _recorded = await _rel_repo.record_edges(
                    tenant_id, investigation_id, _inv.application_id, _rel_edges
                )
                story_log(
                    logger,
                    logging.INFO,
                    "Correlation edges recorded for concluded investigation",
                    comp=COMP_ACTVTY,
                    func="_gate_conclusion_or_fail",
                    stage="conclude",
                    step="correlate",
                    outcome=OUTCOME_COMPLETED,
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    event="correlation edges recorded",
                    outputs={"edges": _recorded},
                    edges=_recorded,
                )
    except Exception as exc:
        story_log(
            logger,
            logging.WARNING,
            "Correlation edge persist skipped; conclusion stands",
            comp=COMP_ACTVTY,
            func="_gate_conclusion_or_fail",
            stage="conclude",
            step="correlate",
            outcome=OUTCOME_SKIPPED,
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            event="correlation edges skipped",
            error=str(exc),
        )
    return InvestigationStatus.COMPLETED


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
def _is_actionable_terminal_reason(text: str) -> bool:
    """Closed-set mirror of next_check_for_abstention's text rows.

    D4 (claim-proof gaps): the router's fallback
    (f"Resolve '{reason}', then re-verify.") is returned for ANY
    unrecognized reason, so comparing the routed output against the
    fallback cannot reliably tell "recognized" from "passthrough" —
    and a targeted "Resolve the ..." row would be discarded by that
    comparison. Filter INPUTS instead: only reasons carrying a
    recognized router signal reach the router at all. This deliberately
    duplicates the router's text predicates (minus the kind=="refuted"
    row, which cannot fire here — kind is always "unverified"); if the
    router gains a text row, this set gains it with a test. Pure.
    """
    import re as _re

    t = (text or "").lower()
    if "all hypotheses rejected or contradicted" in t:
        return True
    if _re.match(r"^\d+ refuting evidence item\(s\) bound", t):
        return True
    if t == "no bound supporting evidence":
        return True
    if "cited evidence_id" in t and "unknown" in t:
        return True
    if "caller enumeration names a caller" in t:
        return True
    if "no cited row shows a call" in t:
        return True
    if "runtime" in t and "behavior" in t:
        return True
    if "java fact" in t:
        return True
    if "empty-search" in t or "absence" in t:
        return True
    return t == "no validated causal chain" or "no causal chain established" in t


def _route_terminal_check(reasons: list[str], generic: str) -> str:
    """Select the terminal next_check from persisted assessment reasons.

    D4 (claim-proof gaps): routes through the existing abstention reason
    router. Only reasons carrying a recognized router signal are passed
    through; ordinary assessment prose is ignored before the router ever
    sees it. The passthrough comparison remains as a second net, pinned
    by test to the router's actual fallback format. Pure; never raises.
    """
    try:
        actionable = [r for r in (reasons or []) if _is_actionable_terminal_reason(r)]
        if not actionable:
            return generic
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )

        routed = VerificationEngine.next_check_for_abstention("unverified", actionable)
        passthrough = f"Resolve '{actionable[0][:300]}', then re-verify."
        if routed and routed != passthrough:
            return routed
        return generic
    except Exception:
        return generic


async def _collect_hypothesis_gate_reasons(
    ctx: Any, tenant_id: str, investigation_id: Any
) -> list[str]:
    """Gate-fail reason strings persisted on hypothesis assessments.

    D4 (claim-proof gaps): reads Hypothesis.assessments[*].reason via the
    established activity-context repository pattern. Verification markers
    are not consulted: next_action does not survive marker serialization.
    Returns [] on any failure — the caller fail-closes to the generic
    terminal directive.
    """
    try:
        hypothesis_repo = getattr(ctx, "hypothesis_repo", None)
        if hypothesis_repo is None:
            return []
        hypotheses = await hypothesis_repo.find_by_investigation_id(tenant_id, investigation_id)
    except Exception:
        return []
    reasons: list[str] = []
    try:
        for hyp in hypotheses or []:
            for asm in getattr(hyp, "assessments", None) or []:
                text = str(getattr(asm, "reason", "") or "").strip()
                if text:
                    reasons.append(text)
    except Exception:
        return []
    return reasons


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def conclude_investigation_activity(
    params: ConcludeInvestigationInput,
) -> GenericActivityResult:
    t0 = tick()
    data = _coerced_data(
        ConcludeInvestigationInput, params, activity="conclude_investigation_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    story_log(
        logger,
        logging.INFO,
        "conclude_investigation_activity started",
        comp=COMP_ACTVTY,
        func="conclude_investigation_activity",
        stage="conclude",
        step="conclude",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        investigation_id=inv_id_probe,
        event="conclude_investigation_activity started",
        inputs={"status": str(data.get("status", "COMPLETED"))},
        status=str(data.get("status", "COMPLETED")),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        status = str(data.get("status", "COMPLETED"))
        reason = str(data.get("reason", "") or "")
        # D3 Phase 0 T3 notes: initialized on every path so the read-only
        # tap can never introduce an unbound name into the native return.
        _conclude_shadow_notes: list[dict[str, Any]] = []
        if not tenant_id or not inv_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and investigation_id required"
            )
        investigation_id = UUID(inv_id_str)
        ctx = _get_ctx()
        inv_repo: Any = getattr(ctx, "investigation_repo", None)
        if inv_repo is not None:
            try:
                inv = await inv_repo.get_by_id(tenant_id, investigation_id)
                if inv is not None:
                    from investigation_agent_platform.domain.investigation.models import (
                        InvestigationStatus,
                    )

                    try:
                        target = InvestigationStatus(status)
                    except ValueError:
                        target = InvestigationStatus.COMPLETED
                    if target == InvestigationStatus.COMPLETED:
                        # F-051: a COMPLETED conclusion requires ConclusionGate
                        # approval — the workflow's requested status alone is not
                        # authority. Denied conclusions become FAILED with the
                        # blockers recorded, never silent COMPLETED.
                        _conclude_shadow_notes = []
                        # B2 kernel wiring (R3b/R3c): shadow evaluates + logs
                        # (never blocks); enforcing denies here, pre-persistence,
                        # on any evaluated kernel DENY. Off mode is a no-op.
                        try:
                            from investigation_agent_platform.application.proof.kernel_conclude_wiring import (
                                kernel_gate_for_conclusion,
                            )

                            _kernel_allowed, _kernel_blockers = await kernel_gate_for_conclusion(
                                tenant_id, investigation_id, ctx
                            )
                        except Exception:
                            _kernel_allowed = False
                            _kernel_blockers = ["kernel-gate-import-failed"]
                        if not _kernel_allowed:
                            target = InvestigationStatus.FAILED
                            reason = "kernel_promotion_denied: " + "; ".join(_kernel_blockers[:5])
                        else:
                            target = await _gate_conclusion_or_fail(
                                tenant_id,
                                investigation_id,
                                ctx,
                                shadow_notes=_conclude_shadow_notes,
                                triage_route=str(data.get("triage_route", "") or ""),
                            )
                    failure_code: str | None = None
                    conclude_reason = f"Workflow concluded with {target.value}"
                    if target == InvestigationStatus.FAILED:
                        # P0-3 honest abstention: epistemic endings (unverified
                        # within budget, budget exhausted, provider-blocked
                        # telemetry) persist an INSUFFICIENT_EVIDENCE
                        # conclusion and complete — FAILED is reserved for
                        # platform/workflow errors. A blocked provider fetch
                        # is a telemetry-deferred handoff, not a crash.
                        _epistemic_reason_lower = (reason or "").strip().lower()
                        epistemic = _epistemic_reason_lower in (
                            "verification_failed_or_limit_reached",
                            "budget_exceeded",
                            "defer_repair_budget_exhausted",
                        ) or _epistemic_reason_lower.startswith("provider_blocked")
                        if epistemic:
                            from investigation_agent_platform.application.investigation.verification import (
                                VERIFIER_VERSION_CURRENT,
                            )
                            from investigation_agent_platform.domain.finding.models import (
                                ConclusionStatus,
                                InvestigationConclusion,
                                classify_terminal_handoff,
                            )

                            finding_repo: Any = getattr(ctx, "finding_repo", None)
                            if finding_repo is not None:
                                try:
                                    _epistemic_reason = str(reason or "").strip().lower()
                                    _epistemic_check = (
                                        "Raise the iteration or evidence budget and "
                                        "resume, then re-verify."
                                        if _epistemic_reason == "budget_exceeded"
                                        else (
                                            "Gather the missing proof kind within the "
                                            "bounded repair budget, then re-verify."
                                            if _epistemic_reason == "defer_repair_budget_exhausted"
                                            else (
                                                "Restore the blocked provider fetch "
                                                "and resume, then re-verify."
                                                if "provider_blocked" in _epistemic_reason
                                                else "Bind supporting evidence and a cited causal "
                                                "chain to a candidate hypothesis, then re-verify."
                                            )
                                        )
                                    )
                                    # P4: budget endings use the same persisted
                                    # assessment reasons as verification-limit endings,
                                    # so a priced next fetch names the actual gap
                                    # instead of generic budget filler. Provider
                                    # blocks skip this routing: no banked gap is
                                    # actionable while the fetch itself is down.
                                    try:
                                        if "provider_blocked" not in _epistemic_reason:
                                            _hyp_reasons = await _collect_hypothesis_gate_reasons(
                                                ctx, tenant_id, investigation_id
                                            )
                                            _epistemic_check = _route_terminal_check(
                                                _hyp_reasons, _epistemic_check
                                            )
                                    except Exception:
                                        pass
                                    _epistemic_handoff = classify_terminal_handoff(
                                        status=ConclusionStatus.INSUFFICIENT_EVIDENCE,
                                        reason=_epistemic_reason,
                                        next_check=_epistemic_check,
                                        limitations=[f"Investigation ended: {reason}."],
                                    )
                                    await finding_repo.save_conclusion(
                                        tenant_id,
                                        InvestigationConclusion(
                                            tenant_id=tenant_id,
                                            investigation_id=investigation_id,
                                            status=ConclusionStatus.INSUFFICIENT_EVIDENCE,
                                            root_cause=None,
                                            supporting_evidence_ids=[],
                                            supporting_hypothesis_ids=[],
                                            contradicting_evidence_ids=[],
                                            confidence=0.0,
                                            limitations=[
                                                (
                                                    f"Investigation ended: {reason}. "
                                                    "No hypothesis met the evidence-bound "
                                                    "verification bar within budget."
                                                )
                                            ],
                                            next_check=_epistemic_check,
                                            recommended_next_steps=list(
                                                _epistemic_handoff.recommended_next_steps or []
                                            ),
                                            handoff=_epistemic_handoff,
                                            verifier_version=VERIFIER_VERSION_CURRENT,
                                        ),
                                    )
                                    # Phase 4.2: abstention volume counter.
                                    try:
                                        _emit_telemetry(
                                            ctx,
                                            tenant_id,
                                            investigation_id,
                                            "record_conclusion_limitation",
                                            "abstention",
                                        )
                                    except Exception:
                                        pass
                                    target = InvestigationStatus.COMPLETED
                                    conclude_reason = (
                                        "Workflow concluded with COMPLETED (INSUFFICIENT_EVIDENCE)"
                                    )
                                except Exception as exc:
                                    story_log(
                                        logger,
                                        logging.WARNING,
                                        "Abstention conclusion persist failed; staying FAILED",
                                        comp=COMP_ACTVTY,
                                        func="conclude_investigation_activity",
                                        stage="conclude",
                                        step="persist_abstention",
                                        outcome=OUTCOME_SKIPPED,
                                        tenant_id=tenant_id,
                                        investigation_id=inv_id_str,
                                        event="abstention persist failed",
                                        error=str(exc),
                                    )
                    if target == InvestigationStatus.FAILED:
                        # Terminal failure record: stamped onto metadata BEFORE
                        # the transition (see _stamp_failure) so it surfaces
                        # via GET detail and GET conclusion — no worker-log
                        # access needed.
                        requested_completed = status.upper() == "COMPLETED"
                        failure_code = _failure_code_for(reason, requested_completed)
                        conclude_reason = f"{conclude_reason} ({failure_code})"
                        inv = _stamp_failure(inv, failure_code, reason)
                    # C-2: loop telemetry (workflow counters + derived repo
                    # counts) stamped for every terminal status, same ride.
                    # D3 T3 conclude-gate pairing: pair the gate notes here,
                    # at the conclude return, with the gate outcome as the
                    # native outcome — the workflow never sees these notes
                    # otherwise, and an unpaired observation is a silent
                    # loss. Envelopes continue the run's receipt chain from
                    # the workflow-passed index; the recomputed summary
                    # covers verify + conclude turns together.
                    _loop_telemetry_input = data.get("loop_telemetry")
                    try:
                        _t3_candidates = [o for o in _conclude_shadow_notes if isinstance(o, dict)]
                        if _t3_candidates and isinstance(_loop_telemetry_input, dict):
                            _t3_native = {
                                "verified": False,
                                "status": str(target.value),
                                "confidence": None,
                                "hypothesis_id": "",
                                "producing_path": "conclude-gate",
                            }
                            _t3_identity: dict[str, Any] = {}
                            _t3_envs, _t3_dups = pair_receipt(
                                _t3_candidates,
                                int(data.get("conclude_iteration", 0) or 0),
                                int(data.get("definition_receipt_start", 0) or 0),
                                _t3_native,
                                _t3_identity,
                            )
                            _combined = (
                                list(_loop_telemetry_input.get("definition_shadow_trace", []) or [])
                                + _t3_envs
                            )
                            _combined_obs = [
                                e.get("observation", e) for e in _combined if isinstance(e, dict)
                            ]
                            _prior_summary = _loop_telemetry_input.get(
                                "shadow_definition_summary", {}
                            )
                            _prior_dups = (
                                _prior_summary.get("duplicate_count", 0)
                                if isinstance(_prior_summary, dict)
                                else 0
                            )
                            _loop_telemetry_input = {
                                **_loop_telemetry_input,
                                "definition_shadow_trace": _combined,
                                "shadow_definition_summary": summarize_shadow_definitions(
                                    _combined_obs,
                                    0,
                                    int(_prior_dups or 0) + int(_t3_dups or 0),
                                    0,
                                ),
                            }
                    except Exception:
                        story_log(
                            logger,
                            logging.ERROR,
                            "conclude T3 shadow pairing failed; verify-turn capture kept",
                            comp=COMP_ACTVTY,
                            func="conclude_investigation_activity",
                            stage="conclude",
                            step="pair_shadow",
                            outcome=OUTCOME_SKIPPED,
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event="conclude T3 shadow pairing failed",
                            exc_info=True,
                        )
                    # D3 omission accounting (C5): if derivation yields None
                    # despite non-empty input, record the in-band flag so the
                    # harness can count the omission (Path A); harness-side
                    # absence detection is Path B.
                    _loop_telemetry_derived = _derive_loop_telemetry(
                        _loop_telemetry_input,
                        await _count_hypothesis_lifecycle(ctx, tenant_id, investigation_id),
                        await _count_evidence_banked(ctx, tenant_id, investigation_id),
                    )
                    _loop_telemetry_omitted = telemetry_omission_flagged(
                        _loop_telemetry_input, _loop_telemetry_derived
                    )
                    inv = _stamp_loop_telemetry(inv, _loop_telemetry_derived)
                    if inv.status != target:
                        # TO_DO-011: idempotency-wired — (investigation_id,
                        # target, version) is deterministic; see transition
                        # activity for the replay contract.
                        _idem_active_key: str | None = None
                        _idem_operation = "activity:conclude"
                        _idem_key = f"conclude:{inv_id_str}:{target.value}:{inv.version}"
                        _replayed, _idem_active_key = await _reserve_or_replay(
                            ctx=ctx,
                            tenant_id=tenant_id,
                            key=_idem_key,
                            request_payload={
                                "tenant_id": tenant_id,
                                "investigation_id": inv_id_str,
                                "target": target.value,
                                "version": inv.version,
                            },
                            operation=_idem_operation,
                            activity="conclude_investigation_activity",
                            investigation_id=inv_id_str,
                        )
                        if _replayed is not None:
                            return GenericActivityResult(
                                success=bool(_replayed.get("success", True)),
                                data=dict(_replayed.get("data") or {"status": target.value}),
                                error=_replayed.get("error"),
                            )
                        _, audit_degraded = await _transition_via_path(
                            tenant_id,
                            inv,
                            target,
                            conclude_reason,
                            inv_repo,
                            getattr(ctx, "transition_repo", None),
                            getattr(ctx, "timeline_repo", None),
                        )
                        if _idem_active_key is not None:
                            await _complete_reservation(
                                ctx=ctx,
                                tenant_id=tenant_id,
                                key=_idem_active_key,
                                operation=_idem_operation,
                                response={
                                    "success": True,
                                    "data": {
                                        "status": target.value,
                                        "audit_degraded": audit_degraded,
                                    },
                                },
                                activity="conclude_investigation_activity",
                                investigation_id=inv_id_str,
                            )
                        story_log(
                            logger,
                            logging.INFO,
                            "conclude_investigation_activity completed",
                            comp=COMP_ACTVTY,
                            func="conclude_investigation_activity",
                            stage="conclude",
                            step="conclude",
                            outcome=OUTCOME_COMPLETED,
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event="conclude_investigation_activity completed",
                            outputs={"outcome": "concluded", "status": target.value},
                            elapsed_ms=elapsed_ms(t0),
                            status=target.value,
                            audit_degraded=audit_degraded,
                        )
                    return GenericActivityResult(
                        success=True,
                        data=attach_shadow_observations(
                            {
                                "status": target.value,
                                "producing_path": "conclude-gate",
                                "loop_telemetry_omitted": _loop_telemetry_omitted,
                            },
                            _conclude_shadow_notes,
                        ),
                    )
            except ApplicationFailure:
                raise
            except Exception as exc:
                story_log(
                    logger,
                    logging.ERROR,
                    "conclude_investigation_activity transition failed",
                    comp=COMP_ACTVTY,
                    func="conclude_investigation_activity",
                    stage="conclude",
                    step="transition",
                    outcome=OUTCOME_FAILED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="conclude_investigation_activity transition failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    exc_info=True,
                )
                raise _application_failure_from_exc(exc) from exc
        return GenericActivityResult(success=True, data={"status": status})
    except ApplicationFailure:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="conclude_investigation_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        raise
    except Exception as exc:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="conclude_investigation_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        story_log(
            logger,
            logging.ERROR,
            "conclude_investigation_activity failed",
            comp=COMP_ACTVTY,
            func="conclude_investigation_activity",
            stage="conclude",
            step="conclude",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="conclude_investigation_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def publish_event_activity(
    params: PublishEventInput,
) -> GenericActivityResult:
    # TO_DO-011: not additionally idempotency-wired — the outbox row itself
    # IS the idempotency record (F-058: durable, idempotent on
    # (tenant_id, idempotency_key) with investigation_id:event_type key);
    # a second reserve/complete layer would be redundant.
    t0 = tick()
    data = _coerced_data(PublishEventInput, params, activity="publish_event_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    story_log(
        logger,
        logging.INFO,
        "publish_event_activity started",
        comp=COMP_ACTVTY,
        func="publish_event_activity",
        stage="publish",
        step="publish",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        investigation_id=inv_id_probe,
        event="publish_event_activity started",
        inputs={"event_type": str(data.get("event", "INVESTIGATION_FINISHED"))},
        event_type=str(data.get("event", "INVESTIGATION_FINISHED")),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        event_type = str(data.get("event", "INVESTIGATION_FINISHED"))
        if not tenant_id or not inv_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and investigation_id required"
            )
        investigation_id = UUID(inv_id_str)
        ctx = _get_ctx()
        publisher: Any = getattr(ctx, "event_publisher", getattr(ctx, "publisher", None))

        inv_repo: Any = getattr(ctx, "investigation_repo", None)
        app_id = "unknown"
        if inv_repo is not None:
            try:
                inv_obj: Any = await inv_repo.get_by_id(tenant_id, investigation_id)
                if inv_obj is not None:
                    app_id = str(inv_obj.application_id)
            except Exception as exc:
                story_log(
                    logger,
                    logging.ERROR,
                    "publish_event_activity investigation fetch failed",
                    comp=COMP_ACTVTY,
                    func="publish_event_activity",
                    stage="publish",
                    step="fetch_investigation",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="publish_event_activity investigation fetch failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    exc_info=True,
                )

        # F-058: transactional outbox — persist the event first (durable,
        # idempotent on (tenant_id, idempotency_key)), then attempt delivery.
        # The row survives publisher outages; dispatch_pending retries later.
        idempotency_key = f"{investigation_id}:{event_type}"
        payload = {
            "investigation_id": str(investigation_id),
            "application_id": app_id,
            "event_type": event_type,
        }
        outbox: Any = getattr(ctx, "outbox_repo", None)
        if outbox is not None:
            try:
                await outbox.enqueue(
                    tenant_id, investigation_id, event_type, idempotency_key, payload
                )
            except Exception as exc:
                story_log(
                    logger,
                    logging.ERROR,
                    "publish_event_activity outbox enqueue failed",
                    comp=COMP_ACTVTY,
                    func="publish_event_activity",
                    stage="publish",
                    step="enqueue",
                    outcome=OUTCOME_FAILED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="publish_event_activity outbox enqueue failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    exc_info=True,
                    idempotency_key=idempotency_key,
                )
                raise _application_failure_from_exc(exc) from exc
            if publisher is not None:
                try:
                    await outbox.dispatch_pending(tenant_id, publisher, batch_size=25)
                except Exception as exc:
                    story_log(
                        logger,
                        logging.WARNING,
                        "publish_event_activity dispatch deferred; event stays queued",
                        comp=COMP_ACTVTY,
                        func="publish_event_activity",
                        stage="publish",
                        step="dispatch",
                        outcome=OUTCOME_SKIPPED,
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="publish_event_activity dispatch deferred",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
            story_log(
                logger,
                logging.INFO,
                "publish_event_activity completed",
                comp=COMP_ACTVTY,
                func="publish_event_activity",
                stage="publish",
                step="publish",
                outcome=OUTCOME_COMPLETED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="publish_event_activity completed",
                outputs={"outcome": "queued", "event_type": event_type},
                elapsed_ms=elapsed_ms(t0),
                event_type=event_type,
            )
            return GenericActivityResult(success=True, data={"event": event_type, "queued": True})

        if publisher is None:
            # No outbox and no publisher wired — explicit failure, not a silent no-op.
            raise ApplicationFailure(
                "No event publisher or outbox configured; cannot publish event",
                type="EventPublisherUnavailable",
                non_retryable=True,
            )

        # Legacy direct-publish fallback (only when no outbox repo is wired,
        # e.g. minimal dev contexts).
        try:
            from investigation_agent_platform.domain.events.base import InvestigationConcluded

            event = InvestigationConcluded(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                application_id=app_id,
                correlation_id=investigation_id,
                final_status=event_type,
                overall_confidence=1.0,
            )
            await publisher.publish_domain_event(tenant_id, event)
        except Exception:
            # Fallback to generic envelope publish.
            try:
                from investigation_agent_platform.ports.messaging.publisher import EventEnvelope

                envelope = EventEnvelope(
                    event_type=event_type,
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    idempotency_key=idempotency_key,
                    payload={"investigation_id": str(investigation_id)},
                )
                await publisher.publish(envelope)
            except Exception as exc:
                story_log(
                    logger,
                    logging.ERROR,
                    "publish_event_activity fallback failed",
                    comp=COMP_ACTVTY,
                    func="publish_event_activity",
                    stage="publish",
                    step="publish",
                    outcome=OUTCOME_FAILED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="publish_event_activity fallback failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    exc_info=True,
                )
                raise _application_failure_from_exc(exc) from exc

        story_log(
            logger,
            logging.INFO,
            "publish_event_activity completed",
            comp=COMP_ACTVTY,
            func="publish_event_activity",
            stage="publish",
            step="publish",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            investigation_id=inv_id_str,
            event="publish_event_activity completed",
            outputs={"outcome": "published", "event_type": event_type},
            elapsed_ms=elapsed_ms(t0),
            event_type=event_type,
        )
        return GenericActivityResult(success=True, data={"event": event_type})
    except ApplicationFailure:
        raise
    except Exception as exc:
        story_log(
            logger,
            logging.ERROR,
            "publish_event_activity failed",
            comp=COMP_ACTVTY,
            func="publish_event_activity",
            stage="publish",
            step="publish",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="publish_event_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise _application_failure_from_exc(exc) from exc
