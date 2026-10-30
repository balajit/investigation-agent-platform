# src/investigation_agent_platform/api/v1/routers/onboarding.py
"""Tenant-onboarding API surface (Phase 4).

Endpoints call the composed onboarding service and return 2xx only after
durable effects commit. When no service is wired (default ``AppContext``),
routes answer 501 so the surface stays explicit about what is enabled.
Deletion preview/approval stay 501 until Phase 5.
"""

from __future__ import annotations

from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, status

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.errors import ApiError
from investigation_agent_platform.api.tenant import require_principal, require_tenant
from investigation_agent_platform.api.v1.schemas.deletion import (
    DeletionApprovalConsumeRequest,
    DeletionApprovalMintRequest,
    DeletionApprovalReceipt,
    DeletionConsumeReceipt,
    DeletionOutcomeRead,
    DeletionPreviewRead,
    DeletionPreviewRequest,
    DeletionRequestRead,
)
from investigation_agent_platform.api.v1.schemas.errors import (
    COMMON_ERROR_RESPONSES,
)
from investigation_agent_platform.api.v1.schemas.onboarding import (
    ApplyResponse,
    ApprovalReceipt,
    ApproveRequest,
    CancelReceipt,
    CancelRequest,
    CapabilityListRead,
    CoverageReportRead,
    EvaluateReadinessRequest,
    IntakePutRead,
    IntakePutRequest,
    LifecycleReceipt,
    OnboardingRequestRead,
    ProbeReceipt,
    ReadinessReceipt,
    RetryReceipt,
    RetryRequest,
    TelemetryCheckItem,
    TelemetryVerifyRequest,
    TelemetryVerifyResponse,
)
from investigation_agent_platform.application.onboarding.adapters import ProbeEvidence
from investigation_agent_platform.application.onboarding.intake import (
    TenantIntakeEnvelope,
)
from investigation_agent_platform.application.onboarding.service import ServiceError

router = APIRouter(tags=["tenant-onboarding"])


def _require_tenant_scope(path_tenant_id: str, authenticated_tenant_id: str) -> None:
    if path_tenant_id != authenticated_tenant_id:
        raise ApiError(
            code="TENANT_MISMATCH",
            http_status=status.HTTP_403_FORBIDDEN,
            message="Tenant authorization mismatch",
        )


def _service() -> Any:
    service = getattr(get_app_context(), "onboarding_service", None)
    if service is None:
        raise ApiError(
            code="ONBOARDING_NOT_IMPLEMENTED",
            http_status=status.HTTP_501_NOT_IMPLEMENTED,
            message="Tenant onboarding operations are not implemented",
        )
    return service


async def _call(method: str, **kwargs: Any) -> dict[str, Any]:
    """Invoke one service method; the service slot itself is untyped."""
    result = await getattr(_service(), method)(**kwargs)
    return cast(dict[str, Any], result)


def _service_error(exc: ServiceError) -> ApiError:
    return ApiError(code=exc.code, http_status=exc.http_status, message=exc.message)


@router.post(
    "/tenants/{tenant_id}/onboarding/requests",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ApplyResponse,
    responses={**COMMON_ERROR_RESPONSES, 202: {"description": "Request accepted and queued."}},
)
async def create_onboarding_request(
    tenant_id: Annotated[str, Path(min_length=1, max_length=64)],
    intake: TenantIntakeEnvelope,
    authenticated_tenant_id: str = Depends(require_tenant),
    actor_sub: str = Depends(require_principal),
    idempotency_key: Annotated[str | None, Header(alias="X-Idempotency-Key")] = None,
) -> ApplyResponse:
    """Validate intake, persist DRAFT, and accept to QUEUED atomically."""
    _require_tenant_scope(tenant_id, authenticated_tenant_id)
    if not idempotency_key:
        raise ApiError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            http_status=status.HTTP_400_BAD_REQUEST,
            message="X-Idempotency-Key header is required",
        )
    try:
        payload = await _service().create_request(
            tenant_id=tenant_id,
            actor_sub=actor_sub,
            idempotency_key=idempotency_key,
            intake=intake,
        )
    except ServiceError as exc:
        raise _service_error(exc) from exc
    return ApplyResponse(
        tenant_id=payload.tenant_id,
        request_id=payload.request_id,
        revision=payload.revision,
        job_id=payload.job_id,
        dispatch_id=payload.dispatch_id,
        workflow_id=payload.workflow_id,
        dispatch_state=payload.dispatch_state,
        status_url=f"/api/v1/tenants/{tenant_id}/onboarding/requests/{payload.request_id}",
    )


@router.put(
    "/tenants/{tenant_id}/onboarding/requests/{request_id}/intake",
    response_model=IntakePutRead,
    responses=COMMON_ERROR_RESPONSES,
)
async def put_onboarding_intake(
    tenant_id: Annotated[str, Path(min_length=1, max_length=64)],
    request_id: Annotated[UUID, Path()],
    body: IntakePutRequest,
    authenticated_tenant_id: str = Depends(require_tenant),
    actor_sub: str = Depends(require_principal),
) -> IntakePutRead:
    """Create the next immutable revision; stale revisions conflict."""
    _require_tenant_scope(tenant_id, authenticated_tenant_id)
    try:
        receipt = await _call(
            "put_intake",
            tenant_id=tenant_id,
            actor_sub=actor_sub,
            request_id=request_id,
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
            intake=TenantIntakeEnvelope(intake=body.intake),
        )
    except ServiceError as exc:
        raise _service_error(exc) from exc
    return IntakePutRead(**receipt)


@router.get(
    "/tenants/{tenant_id}/onboarding/requests/{request_id}",
    response_model=OnboardingRequestRead,
    responses=COMMON_ERROR_RESPONSES,
)
async def get_onboarding_request(
    tenant_id: Annotated[str, Path(min_length=1, max_length=64)],
    request_id: Annotated[UUID, Path()],
    authenticated_tenant_id: str = Depends(require_tenant),
) -> OnboardingRequestRead:
    """Tenant-authorized progress and blockers for one request."""
    _require_tenant_scope(tenant_id, authenticated_tenant_id)
    try:
        status_payload = await _service().get_status(tenant_id, request_id)
    except ServiceError as exc:
        raise _service_error(exc) from exc
    return OnboardingRequestRead(
        tenant_id=status_payload.tenant_id,
        request_id=status_payload.request_id,
        revision=status_payload.revision,
        state=status_payload.state,
        state_version=status_payload.state_version,
        job_id=status_payload.job_id,
        readiness_decision=status_payload.readiness_decision,
        blocking_codes=list(status_payload.blocking_codes),
        registry_status=status_payload.registry_status,
    )


@router.post(
    "/tenants/{tenant_id}/onboarding/requests/{request_id}:approve",
    response_model=ApprovalReceipt,
    responses=COMMON_ERROR_RESPONSES,
)
async def approve_onboarding_request(
    tenant_id: Annotated[str, Path(min_length=1, max_length=64)],
    request_id: Annotated[UUID, Path()],
    body: ApproveRequest,
    authenticated_tenant_id: str = Depends(require_tenant),
    actor_sub: str = Depends(require_principal),
) -> ApprovalReceipt:
    """Approve an exact revision and activate the tenant on READY readiness."""
    _require_tenant_scope(tenant_id, authenticated_tenant_id)
    try:
        receipt = await _call(
            "approve_and_activate",
            tenant_id=tenant_id,
            actor_sub=actor_sub,
            request_id=request_id,
            revision=body.revision,
            idempotency_key=body.idempotency_key,
        )
    except ServiceError as exc:
        raise _service_error(exc) from exc
    return ApprovalReceipt(
        tenant_id=receipt["tenant_id"],
        status=receipt["status"],
        epoch=receipt["epoch"],
        request_id=request_id,
        revision=body.revision,
    )


@router.post(
    "/tenants/{tenant_id}/onboarding/requests/{request_id}:retry",
    response_model=RetryReceipt,
    responses=COMMON_ERROR_RESPONSES,
)
async def retry_onboarding_dispatch(
    tenant_id: Annotated[str, Path(min_length=1, max_length=64)],
    request_id: Annotated[UUID, Path()],
    body: RetryRequest,
    authenticated_tenant_id: str = Depends(require_tenant),
) -> RetryReceipt:
    """Make a RETRYABLE dispatch due immediately. Terminal rows never move."""
    _require_tenant_scope(tenant_id, authenticated_tenant_id)
    _ = request_id
    try:
        receipt = await _call("retry_dispatch", tenant_id=tenant_id, dispatch_id=body.dispatch_id)
    except ServiceError as exc:
        raise _service_error(exc) from exc
    return RetryReceipt(**receipt)


@router.post(
    "/tenants/{tenant_id}/onboarding/requests/{request_id}:cancel",
    response_model=CancelReceipt,
    responses=COMMON_ERROR_RESPONSES,
)
async def cancel_onboarding_request(
    tenant_id: Annotated[str, Path(min_length=1, max_length=64)],
    request_id: Annotated[UUID, Path()],
    body: CancelRequest,
    authenticated_tenant_id: str = Depends(require_tenant),
) -> CancelReceipt:
    """Request cancellation; the workflow converges the row to CANCELED."""
    _require_tenant_scope(tenant_id, authenticated_tenant_id)
    try:
        status_payload = await _service().cancel_request(tenant_id, request_id, body.revision)
    except ServiceError as exc:
        raise _service_error(exc) from exc
    return CancelReceipt(tenant_id=tenant_id, request_id=request_id, state=status_payload.state)


@router.post(
    "/tenants/{tenant_id}:suspend",
    response_model=LifecycleReceipt,
    responses=COMMON_ERROR_RESPONSES,
)
async def suspend_tenant(
    tenant_id: Annotated[str, Path(min_length=1, max_length=64)],
    authenticated_tenant_id: str = Depends(require_tenant),
) -> LifecycleReceipt:
    """Suspend tenant access without deleting data."""
    _require_tenant_scope(tenant_id, authenticated_tenant_id)
    try:
        receipt = await _call("suspend_tenant", tenant_id=tenant_id)
    except ServiceError as exc:
        raise _service_error(exc) from exc
    return LifecycleReceipt(**receipt)


@router.post(
    "/tenants/{tenant_id}:resume",
    response_model=LifecycleReceipt,
    responses=COMMON_ERROR_RESPONSES,
)
async def resume_tenant(
    tenant_id: Annotated[str, Path(min_length=1, max_length=64)],
    authenticated_tenant_id: str = Depends(require_tenant),
) -> LifecycleReceipt:
    """Resume a suspended tenant."""
    _require_tenant_scope(tenant_id, authenticated_tenant_id)
    try:
        receipt = await _call("resume_tenant", tenant_id=tenant_id)
    except ServiceError as exc:
        raise _service_error(exc) from exc
    return LifecycleReceipt(**receipt)


@router.get(
    "/tenants/{tenant_id}/capabilities",
    response_model=CapabilityListRead,
    responses=COMMON_ERROR_RESPONSES,
)
async def list_capabilities(
    tenant_id: Annotated[str, Path(min_length=1, max_length=64)],
    tier: Annotated[str, Query(min_length=1, max_length=32)] = "standard",
    authenticated_tenant_id: str = Depends(require_tenant),
) -> CapabilityListRead:
    """Policy-resolved supported, required, optional, and unsupported capabilities."""
    _require_tenant_scope(tenant_id, authenticated_tenant_id)
    try:
        capabilities = _service().capabilities(tier)
    except ServiceError as exc:
        raise _service_error(exc) from exc
    return CapabilityListRead(tenant_id=tenant_id, tier=tier, capabilities=capabilities)


@router.post(
    "/tenants/{tenant_id}/onboarding/telemetry:verify",
    response_model=TelemetryVerifyResponse,
    responses=COMMON_ERROR_RESPONSES,
)
async def verify_telemetry(
    tenant_id: Annotated[str, Path(min_length=1, max_length=64)],
    body: TelemetryVerifyRequest,
    authenticated_tenant_id: str = Depends(require_tenant),
) -> TelemetryVerifyResponse:
    """Verify inline telemetry endpoint declarations live (no filesystem)."""
    _require_tenant_scope(tenant_id, authenticated_tenant_id)
    try:
        result = await _call(
            "verify_telemetry",
            tenant_id=tenant_id,
            home_service=body.home_service,
            endpoints=body.endpoints,
            mapping_source_id=body.mapping_source_id,
        )
    except ServiceError as exc:
        raise _service_error(exc) from exc
    return TelemetryVerifyResponse(
        tenant_id=result["tenant_id"],
        passed=result["passed"],
        checks=[TelemetryCheckItem(**c) for c in result["checks"]],
    )


@router.post(
    "/tenants/{tenant_id}/onboarding/requests/{request_id}/revisions/{revision}/probes",
    status_code=status.HTTP_201_CREATED,
    response_model=ProbeReceipt,
    responses=COMMON_ERROR_RESPONSES,
)
async def file_probe_evidence(
    tenant_id: Annotated[str, Path(min_length=1, max_length=64)],
    request_id: Annotated[UUID, Path()],
    revision: Annotated[int, Path(ge=1)],
    evidence: ProbeEvidence,
    authenticated_tenant_id: str = Depends(require_tenant),
) -> ProbeReceipt:
    """File one adapter probe evidence pinned to a request revision."""
    _require_tenant_scope(tenant_id, authenticated_tenant_id)
    try:
        receipt = await _call(
            "record_probe_evidence",
            tenant_id=tenant_id,
            request_id=request_id,
            revision=revision,
            evidence=evidence,
        )
    except ServiceError as exc:
        raise _service_error(exc) from exc
    return ProbeReceipt(
        tenant_id=receipt["tenant_id"],
        request_id=request_id,
        revision=revision,
        capability_key=receipt["capability_key"],
        outcome=receipt["outcome"],
    )


@router.post(
    "/tenants/{tenant_id}/onboarding/requests/{request_id}/readiness:evaluate",
    response_model=ReadinessReceipt,
    responses=COMMON_ERROR_RESPONSES,
)
async def evaluate_readiness_report(
    tenant_id: Annotated[str, Path(min_length=1, max_length=64)],
    request_id: Annotated[UUID, Path()],
    body: EvaluateReadinessRequest,
    authenticated_tenant_id: str = Depends(require_tenant),
) -> ReadinessReceipt:
    """Evaluate stored probe evidence and file the readiness report."""
    _require_tenant_scope(tenant_id, authenticated_tenant_id)
    try:
        receipt = await _call(
            "evaluate_and_file_readiness",
            tenant_id=tenant_id,
            request_id=request_id,
            revision=body.revision,
            tier=body.tier,
        )
    except ServiceError as exc:
        raise _service_error(exc) from exc
    return ReadinessReceipt(
        tenant_id=receipt["tenant_id"],
        request_id=request_id,
        revision=body.revision,
        tier=body.tier,
        decision=receipt["decision"],
        blocking_codes=list(receipt["blocking_codes"]),
    )


@router.get(
    "/tenants/{tenant_id}/onboarding/requests/{request_id}/revisions/{revision}/reports/{generation}",
    response_model=CoverageReportRead,
    responses=COMMON_ERROR_RESPONSES,
)
async def get_coverage_report(
    tenant_id: Annotated[str, Path(min_length=1, max_length=64)],
    request_id: Annotated[UUID, Path()],
    revision: Annotated[int, Path(ge=1)],
    generation: Annotated[int, Path(ge=1)],
    authenticated_tenant_id: str = Depends(require_tenant),
) -> CoverageReportRead:
    """Return one filed coverage report by linkage (DEP-10)."""
    _require_tenant_scope(tenant_id, authenticated_tenant_id)
    try:
        receipt = await _call(
            "get_report",
            tenant_id=tenant_id,
            request_id=request_id,
            revision=revision,
            generation=generation,
        )
    except ServiceError as exc:
        raise _service_error(exc) from exc
    return CoverageReportRead(
        tenant_id=receipt["tenant_id"],
        request_id=request_id,
        revision=revision,
        generation=generation,
        report_content_hash=receipt["report_content_hash"],
        report=receipt["report"],
    )


def _cleanup_service() -> Any:
    service = getattr(get_app_context(), "cleanup_service", None)
    if service is None:
        raise ApiError(
            code="CLEANUP_NOT_ENABLED",
            http_status=status.HTTP_501_NOT_IMPLEMENTED,
            message="Deletion preview and approval are not enabled",
        )
    return service


@router.post(
    "/tenants/{tenant_id}:deletion-request",
    status_code=status.HTTP_201_CREATED,
    response_model=DeletionPreviewRead,
    responses=COMMON_ERROR_RESPONSES,
)
async def preview_deletion_request(
    tenant_id: Annotated[str, Path(min_length=1, max_length=64)],
    body: DeletionPreviewRequest,
    authenticated_tenant_id: str = Depends(require_tenant),
) -> DeletionPreviewRead:
    """Enumerate required stores into an exact manifest; gaps block approval."""
    _require_tenant_scope(tenant_id, authenticated_tenant_id)
    try:
        service = _cleanup_service()
        registry = await service.load_registry(body.deployment_id)
        receipt = await service.preview(
            tenant_id=tenant_id,
            application_id=body.application_id,
            environment=body.environment,
            deployment_id=body.deployment_id,
            registry=registry,
            policy_version=body.policy_version,
            requester_id=body.requester_id,
            approver_ids=body.approver_ids,
            backup_id=body.backup_id,
            expires_at=body.expires_at,
        )
    except ServiceError as exc:
        raise _service_error(exc) from exc
    return DeletionPreviewRead(**receipt)


@router.post(
    "/tenants/{tenant_id}/deletion-requests/{manifest_id}:approve-mint",
    status_code=status.HTTP_201_CREATED,
    response_model=DeletionApprovalReceipt,
    responses=COMMON_ERROR_RESPONSES,
)
async def mint_deletion_approval(
    tenant_id: Annotated[str, Path(min_length=1, max_length=64)],
    manifest_id: Annotated[UUID, Path()],
    body: DeletionApprovalMintRequest,
    authenticated_tenant_id: str = Depends(require_tenant),
) -> DeletionApprovalReceipt:
    """Mint a one-time approval token bound to the manifest plan."""
    _require_tenant_scope(tenant_id, authenticated_tenant_id)
    if body.manifest_id != manifest_id:
        raise ApiError(
            code="PLAN_CHANGED",
            http_status=status.HTTP_400_BAD_REQUEST,
            message="Body manifest_id must match the path",
        )
    try:
        receipt = await _cleanup_service().mint_approval(
            tenant_id=tenant_id,
            manifest_id=manifest_id,
            approver_id=body.approver_id,
            requester_id=body.requester_id,
            policy_version=body.policy_version,
            store_registry_version=body.store_registry_version,
            expires_at=body.expires_at,
            nonce=body.nonce,
        )
    except ServiceError as exc:
        raise _service_error(exc) from exc
    return DeletionApprovalReceipt(
        tenant_id=receipt["tenant_id"],
        manifest_id=receipt["manifest_id"],
        approval_token=receipt["approval_token"],
        expires_at=receipt["expires_at"],
        consumed=receipt["consumed"],
    )


@router.post(
    "/deletion-requests/{manifest_id}:approve",
    response_model=DeletionConsumeReceipt,
    responses=COMMON_ERROR_RESPONSES,
)
async def approve_deletion_request(
    manifest_id: Annotated[UUID, Path()],
    body: DeletionApprovalConsumeRequest,
    authenticated_tenant_id: str = Depends(require_tenant),
) -> DeletionConsumeReceipt:
    """Atomically consume an approval and enqueue purge."""
    try:
        receipt = await _cleanup_service().consume_approval(
            tenant_id=authenticated_tenant_id,
            manifest_id=manifest_id,
            presented_token=body.approval_token,
            approver_id=body.approver_id,
        )
    except ServiceError as exc:
        raise _service_error(exc) from exc
    return DeletionConsumeReceipt(
        tenant_id=receipt["tenant_id"],
        manifest_id=receipt["manifest_id"],
        purge_job_id=receipt["purge_job_id"],
        consumed=receipt["consumed"],
    )


@router.get(
    "/deletion-requests/{manifest_id}",
    response_model=DeletionRequestRead,
    responses=COMMON_ERROR_RESPONSES,
)
async def get_deletion_request(
    manifest_id: Annotated[UUID, Path()],
    authenticated_tenant_id: str = Depends(require_tenant),
) -> DeletionRequestRead:
    """Show per-store status and residuals."""
    try:
        receipt = await _cleanup_service().get_deletion(authenticated_tenant_id, manifest_id)
    except ServiceError as exc:
        raise _service_error(exc) from exc
    return DeletionRequestRead(
        tenant_id=receipt["tenant_id"],
        manifest_id=receipt["manifest_id"],
        state=receipt["state"],
        plan_hash=receipt["plan_hash"],
        store_registry_version=receipt["store_registry_version"],
        outcomes=[
            DeletionOutcomeRead(entry_key=o["entry_key"], state=o["state"])
            for o in receipt["outcomes"]
        ],
        blocking_codes=receipt["blocking_codes"],
        expires_at=receipt["expires_at"],
    )
