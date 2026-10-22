"""Inert tenant-onboarding API surface pending service implementation."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Path, status

from investigation_agent_platform.api.errors import ApiError
from investigation_agent_platform.api.tenant import require_tenant
from investigation_agent_platform.api.v1.schemas.errors import (
    COMMON_ERROR_RESPONSES,
    ErrorResponse,
)
from investigation_agent_platform.application.onboarding.intake import TenantIntakeEnvelope

router = APIRouter(tags=["tenant-onboarding"])

_NOT_IMPLEMENTED_RESPONSES = {
    **COMMON_ERROR_RESPONSES,
    501: {"model": ErrorResponse, "description": "Tenant onboarding service is not implemented."},
}


def _require_tenant_scope(path_tenant_id: str, authenticated_tenant_id: str) -> None:
    if path_tenant_id != authenticated_tenant_id:
        raise ApiError(
            code="TENANT_MISMATCH",
            http_status=status.HTTP_403_FORBIDDEN,
            message="Tenant authorization mismatch",
        )


def _not_implemented() -> ApiError:
    return ApiError(
        code="ONBOARDING_NOT_IMPLEMENTED",
        http_status=status.HTTP_501_NOT_IMPLEMENTED,
        message="Tenant onboarding operations are not implemented",
    )


@router.post(
    "/tenants/{tenant_id}/onboarding/requests",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    response_model=ErrorResponse,
    responses=_NOT_IMPLEMENTED_RESPONSES,
)
async def create_onboarding_request(
    tenant_id: Annotated[str, Path(min_length=1, max_length=64)],
    intake: TenantIntakeEnvelope = Body(...),  # noqa: B008 - FastAPI Body idiom
    authenticated_tenant_id: str = Depends(require_tenant),
) -> ErrorResponse:
    """Validate the request shape but do not accept or persist intake."""
    _require_tenant_scope(tenant_id, authenticated_tenant_id)
    raise _not_implemented()


@router.get(
    "/tenants/{tenant_id}/onboarding/requests/{request_id}",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    response_model=ErrorResponse,
    responses=_NOT_IMPLEMENTED_RESPONSES,
)
async def get_onboarding_request(
    tenant_id: Annotated[str, Path(min_length=1, max_length=64)],
    request_id: Annotated[UUID, Path()],
    authenticated_tenant_id: str = Depends(require_tenant),
) -> ErrorResponse:
    """Expose the future request-status path without reading control state."""
    _ = request_id
    _require_tenant_scope(tenant_id, authenticated_tenant_id)
    raise _not_implemented()


@router.post(
    "/tenants/{tenant_id}/onboarding/requests/{request_id}/submit",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    response_model=ErrorResponse,
    responses=_NOT_IMPLEMENTED_RESPONSES,
)
async def submit_onboarding_request(
    tenant_id: Annotated[str, Path(min_length=1, max_length=64)],
    request_id: Annotated[UUID, Path()],
    authenticated_tenant_id: str = Depends(require_tenant),
) -> ErrorResponse:
    """Expose the future submit path without acceptance or dispatch effects."""
    _ = request_id
    _require_tenant_scope(tenant_id, authenticated_tenant_id)
    raise _not_implemented()
