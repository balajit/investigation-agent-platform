"""Exact-fetch execution endpoint for S7 continuations."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, status

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.errors import ApiError, sanitize_extra
from investigation_agent_platform.api.tenant import require_principal, require_tenant
from investigation_agent_platform.api.v1.schemas.continuations import (
    ContinuationExecuteResponse,
)
from investigation_agent_platform.api.v1.schemas.errors import COMMON_ERROR_RESPONSES
from investigation_agent_platform.application.worker.workflows.continuation import (
    ContinuationWorkflowInput,
)
from investigation_agent_platform.domain.continuation.models import (
    S7_CONTINUATION_LEASE_SECONDS,
    S7_CONTINUATION_MAX_ATTEMPTS,
    ContinuationStatus,
    S7Continuation,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["continuations"])


def _response(
    row: S7Continuation, *, replay: bool, error_code: str | None = None
) -> ContinuationExecuteResponse:
    return ContinuationExecuteResponse(
        continuation_id=row.continuation_id,
        investigation_id=row.investigation_id,
        status=row.status,
        action=row.action,
        target=row.target,
        price=row.price,
        idempotent_replay=replay,
        error_code=error_code or row.last_error_code,
    )


@router.post(
    "/investigations/{investigation_id}/continuations/{continuation_id}/execute",
    response_model=ContinuationExecuteResponse,
    responses=COMMON_ERROR_RESPONSES,
    status_code=status.HTTP_202_ACCEPTED,
)
async def execute_continuation(
    investigation_id: str,
    continuation_id: str,
    tenant_id: str = Depends(require_tenant),
    principal_id: str = Depends(require_principal),
) -> ContinuationExecuteResponse:
    try:
        investigation_uuid = UUID(investigation_id)
        continuation_uuid = UUID(continuation_id)
    except ValueError as exc:
        raise ApiError(
            code="INVALID_INVESTIGATION_ID",
            http_status=status.HTTP_400_BAD_REQUEST,
            message="Invalid investigation or continuation id",
        ) from exc

    ctx = get_app_context()
    repo = getattr(ctx, "continuation_repo", None)
    if repo is None:
        raise ApiError(
            code="CONTINUATION_REPOSITORY_UNAVAILABLE",
            http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
            message="Continuation execution is unavailable",
        )
    row = await repo.get(tenant_id, continuation_uuid)
    if row is None:
        raise ApiError(
            code="CONTINUATION_NOT_FOUND", http_status=status.HTTP_404_NOT_FOUND,
            message="Continuation not found",
        )
    investigation = await ctx.investigation_repo.get_by_id(tenant_id, investigation_uuid)
    if (
        investigation is None
        or row.investigation_id != investigation_uuid
        or row.tenant_id != tenant_id
    ):
        raise ApiError(
            code="CONTINUATION_MISMATCH", http_status=status.HTTP_409_CONFLICT,
            message="Continuation does not match the requested investigation",
        )
    conclusion = await ctx.finding_repo.get_conclusion(tenant_id, investigation_uuid)
    handoff = getattr(conclusion, "handoff", None) if conclusion is not None else None
    if handoff is None or getattr(handoff, "version", None) != row.handoff_version:
        raise ApiError(
            code="CONTINUATION_EXPIRED", http_status=status.HTTP_409_CONFLICT,
            message="Continuation source handoff is no longer current",
        )

    if row.status in {
        ContinuationStatus.SUCCEEDED,
        ContinuationStatus.EXPIRED,
        ContinuationStatus.SUPERSEDED,
    }:
        return _response(row, replay=True)

    now = datetime.now(UTC)
    if row.expires_at <= now:
        await repo.set_status(tenant_id, continuation_uuid, "EXPIRED", error_code="CONTINUATION_EXPIRED")
        raise ApiError(
            code="CONTINUATION_EXPIRED", http_status=status.HTTP_409_CONFLICT,
            message="Continuation has expired",
        )
    if row.status == ContinuationStatus.CLAIMED and row.claimed_at is not None:
        if row.claimed_at.timestamp() + S7_CONTINUATION_LEASE_SECONDS > now.timestamp():
            return _response(row, replay=True)
    if (
        row.status in {ContinuationStatus.FAILED, ContinuationStatus.DENIED}
        and row.attempt_count >= S7_CONTINUATION_MAX_ATTEMPTS
    ):
        return _response(row, replay=True, error_code="CONTINUATION_RETRY_EXHAUSTED")

    application_id = getattr(investigation, "application_id", None)
    authorizer = getattr(ctx, "action_authorizer", None)
    allowed = (
        await authorizer.authorize_action(
            tenant_id, row.action, row.target, {"application_id": application_id, "principal_id": principal_id}
        )
        if authorizer is not None
        else False
    )
    if not allowed:
        await repo.set_status(tenant_id, continuation_uuid, "DENIED", error_code="CONTINUATION_AUTHORIZATION_DENIED")
        raise ApiError(
            code="CONTINUATION_AUTHORIZATION_DENIED", http_status=status.HTTP_403_FORBIDDEN,
            message="The stored continuation action is not authorized",
        )

    claimed = await repo.claim(
        tenant_id, continuation_uuid, investigation_uuid, row.handoff_version,
        row.idempotency_key, now, S7_CONTINUATION_LEASE_SECONDS, S7_CONTINUATION_MAX_ATTEMPTS,
    )
    if not claimed:
        current = await repo.get(tenant_id, continuation_uuid)
        if current is not None and current.status == ContinuationStatus.CLAIMED:
            return _response(current, replay=True)
        if current is not None and current.attempt_count >= S7_CONTINUATION_MAX_ATTEMPTS:
            return _response(current, replay=True, error_code="CONTINUATION_RETRY_EXHAUSTED")
        raise ApiError(
            code="CONTINUATION_MISMATCH", http_status=status.HTTP_409_CONFLICT,
            message="Continuation could not be claimed",
        )

    claimed_row = await repo.get(tenant_id, continuation_uuid)
    attempt = claimed_row.attempt_count if claimed_row is not None else row.attempt_count + 1
    workflow_id = f"wf-continuation-{continuation_uuid}-a{attempt}"
    temporal_client = getattr(ctx, "temporal_client", None)
    if temporal_client is None:
        await repo.set_status(tenant_id, continuation_uuid, "FAILED", error_code="CONTINUATION_DISPATCH_UNAVAILABLE")
        raise ApiError(
            code="CONTINUATION_DISPATCH_UNAVAILABLE", http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
            message="Workflow execution engine is unavailable; continuation was claimed and failed",
        )
    try:
        handle = await temporal_client.start_workflow(
            "ContinuationWorkflow",
            ContinuationWorkflowInput(
                tenant_id=tenant_id,
                continuation_id=str(continuation_uuid),
                principal_id=principal_id,
            ),
            id=workflow_id,
            task_queue=getattr(getattr(ctx, "temporal_config", None), "task_queue", "investigation-tasks"),
            memo={"tenant_id": tenant_id, "investigation_id": str(investigation_uuid)},
        )
    except Exception as exc:
        if type(exc).__name__ not in {"WorkflowAlreadyStartedError", "WorkflowExecutionAlreadyStarted"}:
            await repo.set_status(tenant_id, continuation_uuid, "FAILED", error_code="CONTINUATION_DISPATCH_UNAVAILABLE")
            logger.error(
                "Continuation dispatch failed",
                extra=sanitize_extra({"tenant_id": tenant_id, "workflow_id": workflow_id, "error": type(exc).__name__}),
            )
            raise ApiError(
                code="CONTINUATION_DISPATCH_UNAVAILABLE", http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
                message="Continuation workflow could not be started",
            ) from exc
        handle = temporal_client.get_workflow_handle(workflow_id)
    run_id = getattr(handle, "run_id", None)
    await repo.set_status(tenant_id, continuation_uuid, "CLAIMED", run_id=run_id)
    claimed_row = await repo.get(tenant_id, continuation_uuid)
    return _response(claimed_row or row, replay=False)
