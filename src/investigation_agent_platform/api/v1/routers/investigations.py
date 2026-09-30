"""Investigation life-cycle and control-plane router (Part 4, section 4.1).

Read paths read through the investigation repository; write paths dispatch
through the investigation services. The FastAPI dependency provider is wired
via ``api.dependencies`` (in-memory solids by default; swap in the SQLAlchemy
session factory for production).
"""

import hashlib
import json
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.tenant import require_principal, require_tenant
from investigation_agent_platform.domain.common.exceptions import (
    ConcurrencyError,
    IdempotencyConflictError,
    IdempotencyInProgressError,
)
from investigation_agent_platform.domain.investigation.input_requirements import (
    MAX_FULFILLMENT_BYTES,
    InputFulfillment,
    RequirementState,
)
from investigation_agent_platform.domain.investigation.models import (
    InvestigationRequest,
    InvestigationStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["investigations"])


class CreateInvestigationBody(BaseModel):
    # F-061: edge payload bounds — oversized descriptions/parameter blobs are
    # rejected at the API boundary before they reach services, workflows, or
    # model prompts.
    application_id: str = Field(
        ..., min_length=1, max_length=128, description="Target application profile identifier"
    )
    problem_description: str = Field(
        ...,
        min_length=1,
        max_length=8000,
        description="Description of the observed incident or system anomaly",
    )
    session_id: str | None = Field(
        default=None, max_length=256, description="Optional telemetry session correlation ID"
    )
    priority: str = Field(
        default="NORMAL",
        max_length=16,
        description="Investigation priority level (LOW, NORMAL, HIGH, CRITICAL)",
    )
    requested_by: str = Field(default="api-user", max_length=256)
    parameters: dict[str, Any] = Field(
        default_factory=dict, max_length=50, description="Custom parameters for analysis workflow"
    )

    def to_request(self, requested_by: str) -> InvestigationRequest:
        return InvestigationRequest(
            application_id=self.application_id,
            problem_description=self.problem_description,
            session_id=self.session_id or f"sess_{self.application_id}",
            requested_by=requested_by,
            priority=self.priority,
            parameters=self.parameters,
        )


@router.post("/investigations", status_code=status.HTTP_201_CREATED)
async def create_investigation(
    body: CreateInvestigationBody,
    x_tenant_id: str = Depends(require_tenant),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    principal_id: str = Depends(require_principal),
) -> dict[str, Any]:
    """Creates a new investigation specification in database with idempotency guarantee.

    Idempotency (F-013/F-014) is enforced via a durable, DB-unique-constraint
    reservation (safe across any number of API replicas — no process-local
    lock is involved) bound to a canonical hash of the request body: reusing
    a key with a different payload is rejected with 409 rather than silently
    replaying the first response.
    """
    ctx = get_app_context()

    if x_idempotency_key:
        canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        try:
            cached, reserved = await ctx.idempotency_store.reserve_or_get(
                x_tenant_id, x_idempotency_key, request_hash
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except IdempotencyInProgressError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        if not reserved:
            logger.info(
                "Replaying cached investigation creation response for key=%s", x_idempotency_key
            )
            return cached or {}

        service = ctx.create_investigation_service()
        request = body.to_request(requested_by=principal_id)
        investigation = await service.execute(request, tenant_id=x_tenant_id)
        response_payload: dict[str, Any] = {
            "id": str(investigation.id),
            "session_id": investigation.session_id,
            "application_id": investigation.application_id,
            "status": investigation.status.value,
            "tenant_id": x_tenant_id,
            "investigation": investigation.model_dump(mode="json"),
        }
        await ctx.idempotency_store.complete(x_tenant_id, x_idempotency_key, response_payload)
        return response_payload

    service = ctx.create_investigation_service()
    request = body.to_request(requested_by=principal_id)
    investigation = await service.execute(request, tenant_id=x_tenant_id)
    response_payload_direct: dict[str, Any] = {
        "id": str(investigation.id),
        "session_id": investigation.session_id,
        "application_id": investigation.application_id,
        "status": investigation.status.value,
        "tenant_id": x_tenant_id,
        "investigation": investigation.model_dump(mode="json"),
    }
    return response_payload_direct


@router.post("/investigations/{investigation_id}/start", status_code=status.HTTP_202_ACCEPTED)
async def start_investigation_workflow(
    investigation_id: str,
    request: Request,
    x_tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Dispatches and starts the Temporal workflow execution for an existing investigation."""
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid investigation id format"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found")

    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Tenant authorization mismatch"
        )

    correlation_id = getattr(request.state, "correlation_id", None) or request.headers.get(
        "X-Correlation-ID"
    )

    return await _dispatch_workflow(
        ctx, investigation, x_tenant_id, investigation_id, correlation_id
    )


async def _dispatch_workflow(
    ctx: Any,
    investigation: Any,
    tenant_id: str,
    investigation_id: str,
    correlation_id: str | None,
) -> dict[str, Any]:
    """Start the Temporal workflow for an existing investigation (shared by
    manual start and error intake). A failed start is never reported as
    success (F-008)."""
    temporal_client = getattr(ctx, "temporal_client", None)
    if temporal_client is None:
        logger.error(
            "Cannot start workflow: Temporal client is not wired",
            extra={"investigation_id": investigation_id, "correlation_id": correlation_id},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Workflow execution engine is unavailable; investigation was not started",
        )

    # Propagate correlation_id to Temporal via workflow memo/headers.
    try:
        from investigation_agent_platform.application.worker.workflows import RunInvestigationInput

        wf_input = RunInvestigationInput(
            application_id=str(investigation.application_id),
            session_id=getattr(investigation, "session_id", None),
            tenant_id=tenant_id,
            description=getattr(investigation, "problem_description", None),
            correlation_id=correlation_id,
            investigation_id=investigation_id,
        )
        memo = (
            {"correlation_id": correlation_id, "tenant_id": tenant_id} if correlation_id else None
        )
        from datetime import timedelta as _timedelta

        await temporal_client.start_workflow(
            "RunInvestigationWorkflow",
            wf_input,
            id=f"wf-investigation-{investigation_id}",
            task_queue=getattr(
                getattr(ctx, "temporal_config", None), "task_queue", "investigation-tasks"
            ),
            memo=memo,
            # F-055: Temporal workflow execution timeout is the outer boundary
            # for investigation wall-clock; application timestamps alone cannot
            # bound provider calls, retries, or worker restarts.
            execution_timeout=_timedelta(seconds=7200),
            run_timeout=_timedelta(seconds=7200),
        )
    except Exception as exc:
        # A failed start must never be reported as success (F-008): the
        # investigation remains in its current (non-running) persisted state
        # and the caller receives an explicit failure.
        logger.error(
            "Failed to start Temporal workflow",
            extra={
                "error": str(exc),
                "correlation_id": correlation_id,
                "investigation_id": investigation_id,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to start workflow execution; investigation was not started",
        ) from exc

    logger.info(
        "Dispatched Temporal workflow start for investigation=%s",
        investigation_id,
        extra={"correlation_id": correlation_id, "tenant_id": tenant_id},
    )
    return {
        "investigation_id": str(investigation.id),
        "status": "RUNNING",
        "workflow_id": f"wf-investigation-{investigation_id}",
        "correlation_id": correlation_id,
        "message": "Workflow started successfully",
    }


class ErrorIntakeBody(BaseModel):
    """Auto-forwarded error report. Tenant NEVER comes from the body — it is
    derived exclusively from verified authentication (D8 merge safety)."""

    application_id: str = Field(..., min_length=1, max_length=128)
    error_class: str = Field(..., min_length=1, max_length=1024)
    repository: str = Field(default="", max_length=512)
    revision: str = Field(default="", max_length=128)
    failing_symbol: str = Field(default="", max_length=1024)
    caller_symbol: str | None = Field(default=None, max_length=1024)
    top_frame_file: str | None = Field(default=None, max_length=1024)
    problem_description: str = Field(default="", max_length=8000)
    session_id: str | None = Field(default=None, max_length=256)
    severity: str = Field(default="ERROR", max_length=16)
    log_refs: list[str] = Field(default_factory=list, max_length=50)
    trace_refs: list[str] = Field(default_factory=list, max_length=50)


def _intake_service_principals() -> set[str]:
    import os

    raw = os.environ.get("IAP_INTAKE_SERVICE_PRINCIPALS", "")
    return {p.strip() for p in raw.split(",") if p.strip()}


@router.post("/intake/errors", status_code=status.HTTP_202_ACCEPTED)
async def intake_error(
    body: ErrorIntakeBody,
    request: Request,
    x_tenant_id: str = Depends(require_tenant),
    principal_id: str = Depends(require_principal),
) -> dict[str, Any]:
    """Accept an auto-forwarded error, merge-or-fork by code-issue fingerprint,
    and dispatch the workflow (Part 6 D4/D8, Slice 0).

    Service-identity auth only: the principal must appear in
    `IAP_INTAKE_SERVICE_PRINCIPALS` (fail closed when unconfigured).
    """
    if principal_id not in _intake_service_principals():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Intake requires an authorized service principal",
        )
    ctx = get_app_context()
    correlation_id = getattr(request.state, "correlation_id", None) or request.headers.get(
        "X-Correlation-ID"
    )
    try:
        result = await ctx.error_intake_service().intake(
            tenant_id=x_tenant_id,
            application_id=body.application_id,
            error_class=body.error_class,
            repository=body.repository,
            revision=body.revision,
            failing_symbol=body.failing_symbol or body.error_class,
            caller_symbol=body.caller_symbol,
            top_frame_file=body.top_frame_file,
            problem_description=body.problem_description,
            session_id=body.session_id,
            requested_by=principal_id,
            log_refs=body.log_refs,
            trace_refs=body.trace_refs,
        )
    except Exception as exc:
        from investigation_agent_platform.domain.common.exceptions import DomainException

        if isinstance(exc, DomainException):
            raise HTTPException(status_code=exc.http_status_code, detail=exc.message) from exc
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Intake failed"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(
        x_tenant_id, result.investigation_id
    )
    if investigation is None:  # pragma: no cover - defensive; intake just created it
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Intake recorded but investigation not found",
        )
    dispatch = await _dispatch_workflow(
        ctx, investigation, x_tenant_id, str(result.investigation_id), correlation_id
    )
    return {
        **dispatch,
        "session_number": result.session_number,
        "merged": result.merged,
        "code_issue_fingerprint": result.code_issue_fingerprint,
    }


@router.get("/investigations/{investigation_id}")
async def get_investigation(
    investigation_id: str,
    x_tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Retrieves an investigation record scoped to the authorized tenant context."""
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid investigation id format"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found")

    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Tenant authorization mismatch"
        )

    # Part 11.5: surface pending input requirements when suspended, so
    # callers learn exactly what data to supply without a second round-trip.
    pending_requirements: list[dict[str, Any]] = []
    if investigation.status == InvestigationStatus.AWAITING_INPUT:
        input_repo = getattr(ctx, "input_repo", None)
        if input_repo is not None:
            pending = await input_repo.get_pending(x_tenant_id, investigation_uuid)
            pending_requirements = [req.model_dump(mode="json") for req in pending]

    return {
        "investigation": investigation.model_dump(mode="json"),
        "pending_requirements": pending_requirements,
    }


class FulfillInputBody(BaseModel):
    requirement_version: int = Field(ge=1)
    data: dict[str, Any] = Field(max_length=200)


@router.post(
    "/investigations/{investigation_id}/input-requirements/{requirement_id}/fulfill",
    status_code=status.HTTP_202_ACCEPTED,
)
async def fulfill_input_requirement(
    investigation_id: str,
    requirement_id: str,
    body: FulfillInputBody,
    x_tenant_id: str = Depends(require_tenant),
    principal_id: str = Depends(require_principal),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
) -> dict[str, Any]:
    """Fulfill a pending input requirement via Temporal Update (Part 11.5).

    Validation split: this endpoint checks auth, tenant, JSON Schema, size,
    and the durable compare-and-set; the workflow Update validator re-checks
    id/version against in-memory state (validators cannot do I/O). Persist
    order is fulfill-row → FULFILLED → Update, so a failed Update is safely
    replayable under the same idempotency key.
    """
    import hashlib

    if not x_idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Idempotency-Key header is required",
        )
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
        requirement_uuid = UUID(requirement_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid id format"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found")
    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Tenant authorization mismatch"
        )
    if investigation.status != InvestigationStatus.AWAITING_INPUT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Investigation is not awaiting input (status {investigation.status.value})",
        )

    canonical = json.dumps(body.data, sort_keys=True, separators=(",", ":"))
    if len(canonical.encode("utf-8")) > MAX_FULFILLMENT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Fulfillment payload exceeds {MAX_FULFILLMENT_BYTES} bytes",
        )
    request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    try:
        cached, reserved = await ctx.idempotency_store.reserve_or_get(
            x_tenant_id,
            x_idempotency_key,
            request_hash,
            operation="input-fulfill",
            application_id=investigation.application_id,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IdempotencyInProgressError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not reserved:
        logger.info("Replaying cached fulfill-input response for key=%s", x_idempotency_key)
        return cached or {}

    input_repo = getattr(ctx, "input_repo", None)
    if input_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Input requirement store is unavailable",
        )
    requirement = await input_repo.get_by_id(x_tenant_id, requirement_uuid)
    if requirement is None or requirement.investigation_id != investigation_uuid:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Input requirement not found"
        )
    if requirement.state != RequirementState.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Input requirement is {requirement.state.value}, not PENDING",
        )
    if requirement.requirement_version != body.requirement_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Stale requirement version; reload pending requirements",
        )
    if requirement.json_schema:
        import jsonschema  # type: ignore[import-untyped]

        try:
            jsonschema.validate(body.data, requirement.json_schema)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Fulfillment data failed requirement schema: {exc}",
            ) from exc

    fulfillment = InputFulfillment(
        requirement_id=requirement_uuid,
        requirement_version=body.requirement_version,
        tenant_id=x_tenant_id,
        investigation_id=investigation_uuid,
        fulfilled_by=principal_id,
        content_digest=f"sha256:{request_hash}",
        content_bytes=len(canonical.encode("utf-8")),
    )
    await input_repo.record_fulfillment(x_tenant_id, fulfillment)
    try:
        await input_repo.set_state(
            x_tenant_id,
            requirement_uuid,
            body.requirement_version,
            RequirementState.FULFILLED,
        )
    except ConcurrencyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    temporal_client = getattr(ctx, "temporal_client", None)
    if temporal_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Workflow execution engine is unavailable; cannot deliver input",
        )
    try:
        handle = temporal_client.get_workflow_handle(f"wf-investigation-{investigation_id}")
        update_result = await handle.execute_update(
            "fulfill_input",
            args=[str(requirement_uuid), body.requirement_version, body.data],
            id=f"fulfill-{x_idempotency_key}",
        )
    except Exception as exc:
        message = str(exc)
        # Validator rejections carry these markers (see workflow validator);
        # they are caller-fixable conflicts, not infrastructure failures.
        # Message inspection follows the events.py precedent for mapping
        # Temporal delivery failures to HTTP semantics.
        if "no pending input requirement" in message or "stale or unknown" in message:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message) from exc
        logger.error(
            "Failed to deliver fulfill_input update",
            extra={"investigation_id": investigation_id, "error": message},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to deliver input to workflow",
        ) from exc

    response_payload: dict[str, Any] = {
        "investigation_id": investigation_id,
        "requirement_id": requirement_id,
        "status": "FULFILLED",
        "update": update_result if isinstance(update_result, dict) else {},
    }
    await ctx.idempotency_store.complete(
        x_tenant_id,
        x_idempotency_key,
        response_payload,
        operation="input-fulfill",
        application_id=investigation.application_id,
    )
    return response_payload


@router.get("/investigations")
async def list_investigations(
    x_tenant_id: str = Depends(require_tenant),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """Lists open investigations for the tenant (most recent first, best-effort order).

    Backed by `list_open_ids`; full query/filter support needs repository
    listing (downgraded in the Part 4 §4.1 matrix note until then).
    """
    ctx = get_app_context()
    ids = await ctx.investigation_repo.list_open_ids(x_tenant_id)
    items = []
    for inv_id in ids[:limit]:
        inv = await ctx.get_investigation_service().execute(x_tenant_id, inv_id)
        if inv is None or getattr(inv, "tenant_id", x_tenant_id) != x_tenant_id:
            continue
        items.append(
            {
                "investigation_id": str(inv.id),
                "status": inv.status.value,
                "application_id": inv.application_id,
                "created_at": inv.created_at.isoformat() if inv.created_at else None,
                "updated_at": inv.updated_at.isoformat() if inv.updated_at else None,
            }
        )
    return {"items": items, "total": len(items)}


@router.get("/investigations/{investigation_id}/conclusion")
async def get_conclusion(
    investigation_id: str,
    x_tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Fetches the terminal root-cause conclusion; null while unconcluded."""
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid investigation id format"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found")

    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Tenant authorization mismatch"
        )

    conclusion = getattr(investigation, "conclusion", None)
    if conclusion is None:
        # Part 11.2: the in-flight aggregate never carries a persisted
        # conclusion (it's written directly to finding_repo by the worker
        # activity, never round-tripped through the Investigation aggregate).
        # Read it from the durable store so this endpoint reflects reality
        # instead of always returning null once concluded.
        finding_repo = getattr(ctx, "finding_repo", None)
        if finding_repo is not None:
            conclusion = await finding_repo.get_conclusion(x_tenant_id, investigation_uuid)
    return {
        "investigation_id": investigation_id,
        "status": investigation.status.value,
        "conclusion": conclusion.model_dump(mode="json") if conclusion is not None else None,
    }


@router.get("/investigations/{investigation_id}/findings")
async def list_findings(
    investigation_id: str,
    x_tenant_id: str = Depends(require_tenant),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """Retrieves paginated findings for an investigation (Part 11.2)."""
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid investigation id format"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found")
    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Tenant authorization mismatch"
        )

    finding_repo = getattr(ctx, "finding_repo", None)
    if finding_repo is None:
        return {"items": [], "total": 0, "offset": offset, "limit": limit}
    findings, total = await finding_repo.list_findings(
        x_tenant_id, investigation_uuid, limit=limit, offset=offset
    )
    return {
        "items": [f.model_dump(mode="json") for f in findings],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.post("/investigations/{investigation_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_investigation(
    investigation_id: str,
    request: Request,
    x_tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Redispatches the Temporal workflow for a FAILED investigation."""
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid investigation id format"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found")

    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Tenant authorization mismatch"
        )
    if investigation.status != InvestigationStatus.FAILED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only FAILED investigations can be retried (status is {investigation.status.value})",
        )

    correlation_id = getattr(request.state, "correlation_id", None) or request.headers.get(
        "X-Correlation-ID"
    )
    return await _dispatch_workflow(
        ctx, investigation, x_tenant_id, investigation_id, correlation_id
    )


@router.post("/investigations/{investigation_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_investigation(
    investigation_id: str,
    reason: str = Query(default="API cancellation requested", max_length=1000),
    x_tenant_id: str = Depends(require_tenant),
) -> dict[str, str]:
    """Issues cancellation signal to Temporal workflow execution and updates persistence status."""
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid investigation id format"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found")

    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Tenant authorization mismatch"
        )

    await ctx.cancel_investigation_service().execute(x_tenant_id, investigation_uuid, reason=reason)
    return {"investigation_id": investigation_id, "status": "CANCELLING", "reason": reason}
