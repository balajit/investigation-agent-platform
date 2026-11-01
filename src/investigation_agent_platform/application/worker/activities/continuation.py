"""Temporal boundaries for exact S7 continuation work."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from temporalio import activity

from investigation_agent_platform.domain.timeline.models import (
    TimelineEvent,
    TimelineEventType,
)

from .common import _coerced_data
from .execution import execute_action_activity
from .inputs import (
    ConcludeInvestigationInput,
    ContinuationReasoningInput,
    ExecuteActionInput,
    ExecuteContinuationInput,
    GenericActivityResult,
    ReasonInput,
    TransitionInvestigationInput,
    VerifyRootCauseInput,
    _get_ctx,
)
from .lifecycle import conclude_investigation_activity, transition_investigation_activity
from .reasoning import reason_activity
from .verification import verify_root_cause_activity

S7_REASONING_LEASE_SECONDS = 3600


def normalize_correlation_id(value: str | None) -> tuple[UUID | None, str | None]:
    """Return a UUID correlation and a bounded non-UUID request attribute."""
    text = str(value or "").strip()
    if not text:
        return None, None
    try:
        return UUID(text), None
    except (ValueError, TypeError, AttributeError):
        return None, text[:256]


async def _timeline_log(
    ctx: Any,
    tenant_id: str,
    investigation_id: UUID,
    description: str,
    continuation_id: UUID,
    correlation_id: str | None,
    attributes: dict[str, Any] | None = None,
) -> None:
    repo = getattr(ctx, "timeline_repo", None)
    if repo is None:
        return
    normalized, request_value = normalize_correlation_id(correlation_id)
    event_attributes = {"continuation_id": str(continuation_id), **(attributes or {})}
    if request_value is not None:
        event_attributes["request_correlation_id"] = request_value
    await repo.append(
        tenant_id,
        TimelineEvent(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            timestamp=datetime.now(UTC),
            event_type=TimelineEventType.LOG_EVENT,
            producer="s7-continuation",
            description=description[:2048],
            correlation_id=normalized,
            attributes=event_attributes,
        ),
        investigation_id,
    )


def _typed_failure(continuation_id: str, code: str, action: str = "") -> GenericActivityResult:
    return GenericActivityResult(
        success=False,
        data={"continuation_id": continuation_id, "error_code": code},
        error=code,
    )


async def _fail_reopened_investigation(
    ctx: Any, tenant_id: str, investigation_id: UUID, reason: str
) -> None:
    """Return a failed reasoning re-entry to an honest terminal state."""
    try:
        investigation_repo = getattr(ctx, "investigation_repo", None)
        investigation = (
            await investigation_repo.get_by_id(tenant_id, investigation_id)
            if investigation_repo is not None
            else None
        )
        if investigation is None:
            return
        from investigation_agent_platform.domain.investigation.models import InvestigationStatus

        if investigation.status == InvestigationStatus.INVESTIGATING:
            await transition_investigation_activity(
                TransitionInvestigationInput(
                    tenant_id=tenant_id,
                    investigation_id=str(investigation_id),
                    target_status=InvestigationStatus.FAILED.value,
                    reason=reason,
                    actor="SYSTEM",
                    failure_code=reason,
                    failure_reason=reason,
                )
            )
    except Exception:
        return


@activity.defn
async def execute_continuation_activity(
    params: ExecuteContinuationInput,
) -> GenericActivityResult:
    """Reload the exact durable action and route it through normal execution."""
    data = _coerced_data(ExecuteContinuationInput, params, activity="execute_continuation_activity")
    tenant_id = str(data.get("tenant_id", ""))
    continuation_id_text = str(data.get("continuation_id", ""))
    try:
        continuation_id = UUID(continuation_id_text)
    except (ValueError, TypeError):
        return _typed_failure(continuation_id_text, "CONTINUATION_ID_INVALID")
    ctx = _get_ctx()
    repo = getattr(ctx, "continuation_repo", None)
    if repo is None:
        return _typed_failure(continuation_id_text, "CONTINUATION_REPOSITORY_UNAVAILABLE")
    continuation = await repo.get(tenant_id, continuation_id)
    if continuation is None:
        return _typed_failure(continuation_id_text, "CONTINUATION_NOT_FOUND")
    if str(continuation.tenant_id) != tenant_id:
        return _typed_failure(continuation_id_text, "CONTINUATION_TENANT_MISMATCH")
    from investigation_agent_platform.application.investigation.continuation import (
        continuation_idempotency_key,
    )
    from investigation_agent_platform.domain.continuation.models import (
        S7_CONTINUATION_VERSION_CURRENT,
    )

    if continuation.version != S7_CONTINUATION_VERSION_CURRENT:
        return _typed_failure(continuation_id_text, "CONTINUATION_VERSION_UNSUPPORTED")
    if continuation_idempotency_key(
        tenant_id,
        continuation.investigation_id,
        continuation.handoff_version,
        continuation.action,
        continuation.parameters,
    ) != continuation.idempotency_key:
        return _typed_failure(continuation_id_text, "CONTINUATION_PAYLOAD_MISMATCH")
    if str(getattr(continuation.status, "value", continuation.status)) != "CLAIMED":
        return _typed_failure(continuation_id_text, "CONTINUATION_NOT_CLAIMED")

    investigation_id = continuation.investigation_id
    investigation_repo = getattr(ctx, "investigation_repo", None)
    investigation = (
        await investigation_repo.get_by_id(tenant_id, investigation_id)
        if investigation_repo is not None
        else None
    )
    if investigation is None:
        await repo.set_status(tenant_id, continuation_id, "FAILED", error_code="INVESTIGATION_NOT_FOUND")
        return _typed_failure(continuation_id_text, "INVESTIGATION_NOT_FOUND", continuation.action)

    loop_version = ""
    try:
        from investigation_agent_platform.domain.investigation.loop_version import (
            resolve_loop_version,
        )

        loop_version = resolve_loop_version(getattr(investigation, "metadata", None))
    except Exception:
        loop_version = ""

    result = await execute_action_activity(
        ExecuteActionInput(
            tenant_id=tenant_id,
            investigation_id=str(investigation_id),
            principal_id=str(data.get("principal_id") or "worker"),
            action=str(continuation.action),
            parameters=dict(continuation.parameters),
            loop_version=loop_version,
        )
    )
    if not result.success:
        error_code = str((result.data or {}).get("error_code") or "CONTINUATION_ACTION_FAILED")
        await repo.set_status(tenant_id, continuation_id, "FAILED", error_code=error_code)
        return GenericActivityResult(
            success=False,
            data={"continuation_id": continuation_id_text, "error_code": error_code},
            error=result.error or error_code,
        )

    evidence_added = int((result.data or {}).get("items_count", 0) or 0)
    await repo.set_status(
        tenant_id,
        continuation_id,
        "SUCCEEDED",
        completed_at=datetime.now(UTC),
    )
    acquired = await repo.acquire_reasoning_lease(
        tenant_id,
        investigation_id,
        continuation_id,
        int(continuation.attempt_count),
        datetime.now(UTC),
        S7_REASONING_LEASE_SECONDS,
    )
    if not acquired:
        await repo.set_status(
            tenant_id,
            continuation_id,
            "SUCCEEDED",
            error_code="CONTINUATION_REASONING_ALREADY_ACTIVE",
        )
        await _timeline_log(
            ctx,
            tenant_id,
            investigation_id,
            "Continuation fetch succeeded; continuation reasoning is already active",
            continuation_id,
            data.get("correlation_id"),
            {"error_code": "CONTINUATION_REASONING_ALREADY_ACTIVE"},
        )
    return GenericActivityResult(
        success=True,
        data={
            "continuation_id": continuation_id_text,
            "investigation_id": str(investigation_id),
            "action": str(continuation.action),
            "attempt_count": int(continuation.attempt_count),
            "evidence_added": evidence_added,
            "reasoning_lease_acquired": acquired,
        },
    )


@activity.defn
async def continuation_reasoning_activity(
    params: ContinuationReasoningInput,
) -> GenericActivityResult:
    """Run a fresh-context reason/verify/conclude pass for a fetched continuation."""
    data = _coerced_data(
        ContinuationReasoningInput, params, activity="continuation_reasoning_activity"
    )
    ctx = _get_ctx()
    repo = getattr(ctx, "continuation_repo", None)
    tenant_id = str(data.get("tenant_id", ""))
    try:
        investigation_id = UUID(str(data.get("investigation_id", "")))
        continuation_id = UUID(str(data.get("continuation_id", "")))
    except (ValueError, TypeError):
        return _typed_failure(str(data.get("continuation_id", "")), "CONTINUATION_ID_INVALID")
    try:
        if repo is None or not callable(getattr(repo, "replace_conclusion_with_history", None)):
            return _typed_failure(str(continuation_id), "CONTINUATION_REASONING_REENTRY_UNAVAILABLE")
        continuation = await repo.get(tenant_id, continuation_id)
        if continuation is None or continuation.investigation_id != investigation_id:
            return _typed_failure(str(continuation_id), "CONTINUATION_STATE_UNAVAILABLE")
        if str(getattr(continuation.status, "value", continuation.status)) != "SUCCEEDED":
            return _typed_failure(str(continuation_id), "CONTINUATION_NOT_SUCCEEDED")
        inv_repo = getattr(ctx, "investigation_repo", None)
        checkpoint_repo = getattr(ctx, "checkpoint_repo", None)
        finding_repo = getattr(ctx, "finding_repo", None)
        investigation = (
            await inv_repo.get_by_id(tenant_id, investigation_id) if inv_repo is not None else None
        )
        conclusion = (
            await finding_repo.get_conclusion(tenant_id, investigation_id)
            if finding_repo is not None and callable(getattr(finding_repo, "get_conclusion", None))
            else None
        )
        checkpoint = (
            await checkpoint_repo.get_latest_checkpoint(tenant_id, investigation_id)
            if checkpoint_repo is not None
            else None
        )
        if investigation is None or conclusion is None or checkpoint is None:
            await repo.set_status(
                tenant_id, continuation_id, "FAILED", error_code="CONTINUATION_STATE_UNAVAILABLE"
            )
            return _typed_failure(str(continuation_id), "CONTINUATION_STATE_UNAVAILABLE")

        from investigation_agent_platform.domain.investigation.models import (
            InvestigationState,
            InvestigationStatus,
        )

        if investigation.status in {InvestigationStatus.COMPLETED, InvestigationStatus.FAILED}:
            transition = await transition_investigation_activity(
                TransitionInvestigationInput(
                    tenant_id=tenant_id,
                    investigation_id=str(investigation_id),
                    target_status=InvestigationStatus.INVESTIGATING.value,
                    reason="S7 continuation reasoning re-entry",
                    actor="SYSTEM",
                )
            )
            if not transition.success:
                return _typed_failure(str(continuation_id), "CONTINUATION_REOPEN_FAILED")

        state = checkpoint if isinstance(checkpoint, dict) else {}
        try:
            InvestigationState.model_validate(state)
        except Exception:
            await repo.set_status(
                tenant_id, continuation_id, "FAILED", error_code="CONTINUATION_STATE_UNAVAILABLE"
            )
            return _typed_failure(str(continuation_id), "CONTINUATION_STATE_UNAVAILABLE")
        history = state.get("tool_history", []) if isinstance(state, dict) else []
        recent_actions = [
            item for item in history[-10:] if isinstance(item, dict)
        ]
        loop_version = str(data.get("loop_version", "") or "")
        if not loop_version:
            try:
                from investigation_agent_platform.domain.investigation.loop_version import (
                    resolve_loop_version,
                )

                loop_version = resolve_loop_version(getattr(investigation, "metadata", None))
            except Exception:
                loop_version = ""
        ctx._continuation_conclusion_id = continuation_id
        reason = await reason_activity(
            ReasonInput(
                tenant_id=tenant_id,
                investigation_id=str(investigation_id),
                iteration=int(getattr(continuation, "attempt_count", 0)),
                loop_version=loop_version,
                recent_actions=recent_actions,
                knowledge_orientation=[],
            )
        )
        if not reason.success:
            await _fail_reopened_investigation(ctx, tenant_id, investigation_id, "CONTINUATION_REASONING_FAILED")
            await repo.set_status(
                tenant_id, continuation_id, "FAILED", error_code="CONTINUATION_REASONING_FAILED"
            )
            return _typed_failure(str(continuation_id), "CONTINUATION_REASONING_FAILED")
        reason_data = reason.data or {}
        recent_actions.append(
            {
                "iteration": int(reason_data.get("iteration", 0) or 0),
                "action": str(reason_data.get("next_action", "") or ""),
                "readiness": reason_data.get("conclusion_readiness"),
                "observations": len(reason_data.get("observations", []) or []),
            }
        )
        verify = await verify_root_cause_activity(
            VerifyRootCauseInput(
                tenant_id=tenant_id,
                investigation_id=str(investigation_id),
                triage_route=str(
                    (getattr(investigation, "metadata", None) or {}).get("triage_route", "UNKNOWN")
                ),
                recent_actions=recent_actions,
                iteration=int(getattr(continuation, "attempt_count", 0)),
            )
        )
        if not verify.success:
            await _fail_reopened_investigation(ctx, tenant_id, investigation_id, "CONTINUATION_VERIFICATION_FAILED")
            await repo.set_status(
                tenant_id, continuation_id, "FAILED", error_code="CONTINUATION_VERIFICATION_FAILED"
            )
            return _typed_failure(str(continuation_id), "CONTINUATION_VERIFICATION_FAILED")
        conclude = await conclude_investigation_activity(
            ConcludeInvestigationInput(
                tenant_id=tenant_id,
                investigation_id=str(investigation_id),
                status="COMPLETED" if verify.data.get("verified") else "FAILED",
                preferred_candidate_id=str(verify.data.get("hypothesis_id", "") or ""),
                reason=str(verify.data.get("reason", "") or "continuation_reverification"),
                triage_route=str(
                    (getattr(investigation, "metadata", None) or {}).get("triage_route", "UNKNOWN")
                ),
                conclude_iteration=int(getattr(continuation, "attempt_count", 0)),
                loop_telemetry={
                    "continuation_id": str(continuation_id),
                    "reasoning_iteration": int(reason_data.get("iteration", 0) or 0),
                    "evidence_banked_total": len(
                        await ctx.evidence_repo.find_by_investigation_id(
                            tenant_id, investigation_id
                        )
                    ),
                },
            )
        )
        if not conclude.success:
            await _fail_reopened_investigation(ctx, tenant_id, investigation_id, "CONTINUATION_CONCLUSION_FAILED")
            await repo.set_status(
                tenant_id,
                continuation_id,
                "FAILED",
                error_code="CONTINUATION_CONCLUSION_FAILED",
            )
            return _typed_failure(str(continuation_id), "CONTINUATION_CONCLUSION_FAILED")
        return GenericActivityResult(
            success=True,
            data={"continuation_id": str(continuation_id), "status": conclude.data.get("status")},
        )
    except Exception:
        try:
            if repo is not None:
                await repo.set_status(
                    tenant_id,
                    continuation_id,
                    "FAILED",
                    error_code="CONTINUATION_REASONING_FAILED",
                )
        except Exception:
            pass
        return _typed_failure(str(continuation_id), "CONTINUATION_REASONING_FAILED")
    finally:
        try:
            if repo is not None:
                await repo.release_reasoning_lease(tenant_id, investigation_id)
        except Exception:
            pass
        if getattr(ctx, "_continuation_conclusion_id", None) == continuation_id:
            del ctx._continuation_conclusion_id
