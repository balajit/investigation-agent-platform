"""Evidence router (Part 4, section 4.1). Read/list paths over normalized evidence."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.errors import ApiError
from investigation_agent_platform.api.tenant import require_tenant
from investigation_agent_platform.api.v1.schemas.envelopes import ListEnvelope
from investigation_agent_platform.api.v1.schemas.errors import COMMON_ERROR_RESPONSES
from investigation_agent_platform.api.v1.schemas.evidence import (
    EvidenceDetail,
    EvidenceSharingResponse,
)
from investigation_agent_platform.domain.evidence.models import Evidence

router = APIRouter(tags=["evidence"])


@router.get(
    "/investigations/{investigation_id}/evidence",
    response_model=ListEnvelope[Evidence],
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def list_evidence(
    investigation_id: str,
    x_tenant_id: str = Depends(require_tenant),
    limit: int = Query(default=50, ge=1, le=200),
) -> ListEnvelope[Evidence]:
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise ApiError(
            code="INVALID_INVESTIGATION_ID",
            http_status=status.HTTP_400_BAD_REQUEST,
            message="Invalid investigation id",
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise ApiError(
            code="INVESTIGATION_NOT_FOUND",
            http_status=status.HTTP_404_NOT_FOUND,
            message="Investigation not found",
        )

    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise ApiError(
            code="TENANT_MISMATCH",
            http_status=status.HTTP_403_FORBIDDEN,
            message="Tenant authorization mismatch",
        )

    items = await ctx.evidence_repo.find_by_investigation_id(x_tenant_id, investigation_uuid)
    return ListEnvelope[Evidence].model_validate(
        {"items": [e.model_dump(mode="json") for e in items[:limit]], "total": len(items)}
    )


@router.get(
    "/investigations/{investigation_id}/evidence/sharing",
    response_model=EvidenceSharingResponse,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def evidence_sharing(
    investigation_id: str,
    x_tenant_id: str = Depends(require_tenant),
) -> EvidenceSharingResponse:
    """Which of this investigation's evidence rows are shared canonical rows
    (G6): key → {shared, investigations}. Owned-only rows are absent."""
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise ApiError(
            code="INVALID_INVESTIGATION_ID",
            http_status=status.HTTP_400_BAD_REQUEST,
            message="Invalid investigation id format",
        ) from exc
    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise ApiError(
            code="INVESTIGATION_NOT_FOUND",
            http_status=status.HTTP_404_NOT_FOUND,
            message="Investigation not found",
        )
    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise ApiError(
            code="TENANT_MISMATCH",
            http_status=status.HTTP_403_FORBIDDEN,
            message="Tenant authorization mismatch",
        )
    items = await ctx.evidence_repo.find_by_investigation_id(x_tenant_id, investigation_uuid)
    repo = getattr(ctx, "evidence_repo", None)
    if repo is None or not hasattr(repo, "sharing_info"):
        return {}
    keys = [str(getattr(e, "evidence_id", "")) for e in items if getattr(e, "evidence_id", None)]
    links = await repo.sharing_info(x_tenant_id, keys)
    return EvidenceSharingResponse.model_validate({"links": links})


@router.get(
    "/investigations/{investigation_id}/evidence/{evidence_id}",
    response_model=EvidenceDetail,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def get_evidence(
    investigation_id: str,
    evidence_id: str,
    x_tenant_id: str = Depends(require_tenant),
) -> EvidenceDetail:
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise ApiError(
            code="INVALID_INVESTIGATION_ID",
            http_status=status.HTTP_400_BAD_REQUEST,
            message="Invalid investigation id",
        ) from exc
    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise ApiError(
            code="INVESTIGATION_NOT_FOUND",
            http_status=status.HTTP_404_NOT_FOUND,
            message="Investigation not found",
        )
    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise ApiError(
            code="TENANT_MISMATCH",
            http_status=status.HTTP_403_FORBIDDEN,
            message="Tenant authorization mismatch",
        )
    try:
        evidence_uuid = UUID(evidence_id)
    except ValueError as exc:
        raise ApiError(
            code="INVALID_EVIDENCE_ID",
            http_status=status.HTTP_400_BAD_REQUEST,
            message="Invalid evidence id",
        ) from exc
    evidence = await ctx.evidence_repo.get_by_id(x_tenant_id, evidence_uuid)
    if not evidence:
        raise ApiError(
            code="EVIDENCE_NOT_FOUND",
            http_status=status.HTTP_404_NOT_FOUND,
            message="Evidence not found",
        )
    return EvidenceDetail.model_validate({"evidence": evidence.model_dump(mode="json")})
