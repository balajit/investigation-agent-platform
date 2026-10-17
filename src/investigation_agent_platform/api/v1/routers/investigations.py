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

from fastapi import APIRouter, Depends, Header, Query, Request, status

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.dependencies import (
    is_worker_visible_store as isWorkerVisibleStore,
)
from investigation_agent_platform.api.errors import ApiError, sanitize_extra
from investigation_agent_platform.api.tenant import require_principal, require_tenant
from investigation_agent_platform.api.v1.schemas.envelopes import ListEnvelope
from investigation_agent_platform.api.v1.schemas.errors import COMMON_ERROR_RESPONSES
from investigation_agent_platform.api.v1.schemas.investigations import (
    CancelAck,
    ConclusionResponse,
    CreateInvestigationBody,
    CreateInvestigationResponse,
    DispatchAck,
    ErrorIntakeAck,
    ErrorIntakeBody,
    FulfillAck,
    FulfillInputBody,
    InvestigationDetailResponse,
    InvestigationListItemView,
    ReprocessBody,
)
from investigation_agent_platform.api.validation import NO_NUL_PATTERN
from investigation_agent_platform.domain.common.exceptions import (
    ConcurrencyError,
    IdempotencyConflictError,
    IdempotencyInProgressError,
)
from investigation_agent_platform.domain.finding.models import Finding
from investigation_agent_platform.domain.investigation.input_requirements import (
    MAX_FULFILLMENT_BYTES,
    InputFulfillment,
    RequirementState,
)
from investigation_agent_platform.domain.investigation.models import ActorType, InvestigationStatus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["investigations"])


# Router-local input models moved to api.v1.schemas.investigations
# (TO_DO-018); re-exported here so existing imports keep working.
__all__ = [
    "CreateInvestigationBody",
    "ErrorIntakeBody",
    "FulfillInputBody",
    "ReprocessBody",
    "router",
]


@router.post(
    "/investigations",
    response_model=CreateInvestigationResponse,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
    status_code=status.HTTP_201_CREATED,
)
async def create_investigation(
    body: CreateInvestigationBody,
    x_tenant_id: str = Depends(require_tenant),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    principal_id: str = Depends(require_principal),
) -> CreateInvestigationResponse:
    """Creates a new investigation specification in database with idempotency guarantee.

    Idempotency (F-013/F-014) is enforced via a durable, DB-unique-constraint
    reservation (safe across any number of API replicas — no process-local
    lock is involved) bound to a canonical hash of the request body: reusing
    a key with a different payload is rejected with 409 rather than silently
    replaying the first response.
    """
    ctx = get_app_context()

    def _response_for(investigation: Any) -> CreateInvestigationResponse:
        return CreateInvestigationResponse.model_validate(
            {
                "id": str(investigation.id),
                "session_id": investigation.session_id,
                "application_id": investigation.application_id,
                "status": investigation.status.value,
                "tenant_id": x_tenant_id,
                "investigation": investigation.model_dump(mode="json"),
                # §2: disposition stashed in metadata by the create service.
                "disposition": (investigation.metadata or {}).get("disposition"),
            }
        )

    if x_idempotency_key:
        canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        try:
            cached, reserved = await ctx.idempotency_store.reserve_or_get(
                x_tenant_id, x_idempotency_key, request_hash
            )
        except IdempotencyConflictError as exc:
            raise ApiError(
                code="IDEMPOTENCY_CONFLICT", http_status=status.HTTP_409_CONFLICT, message=str(exc)
            ) from exc
        except IdempotencyInProgressError as exc:
            raise ApiError(
                code="IDEMPOTENCY_CONFLICT", http_status=status.HTTP_409_CONFLICT, message=str(exc)
            ) from exc

        if not reserved:
            logger.info(
                "Replaying cached investigation creation response for key=%s", x_idempotency_key
            )
            return CreateInvestigationResponse.model_validate(cached or {})

        service = ctx.create_investigation_service()
        request = body.to_request(requested_by=principal_id)
        investigation = await service.execute(request, tenant_id=x_tenant_id)
        response_payload = _response_for(investigation)
        await ctx.idempotency_store.complete(
            x_tenant_id, x_idempotency_key, response_payload.model_dump(mode="json", exclude_unset=True)
        )
        return response_payload

    service = ctx.create_investigation_service()
    request = body.to_request(requested_by=principal_id)
    investigation = await service.execute(request, tenant_id=x_tenant_id)
    return _response_for(investigation)


@router.post(
    "/investigations/{investigation_id}/start",
    response_model=DispatchAck,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_investigation_workflow(
    investigation_id: str,
    request: Request,
    x_tenant_id: str = Depends(require_tenant),
) -> DispatchAck:
    """Dispatches and starts the Temporal workflow execution for an existing investigation."""
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise ApiError(
            code="INVALID_INVESTIGATION_ID", http_status=status.HTTP_400_BAD_REQUEST, message="Invalid investigation id format"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise ApiError(
            code="INVESTIGATION_NOT_FOUND", http_status=status.HTTP_404_NOT_FOUND, message="Investigation not found"
        )

    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise ApiError(
            code="TENANT_MISMATCH", http_status=status.HTTP_403_FORBIDDEN, message="Tenant authorization mismatch"
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
    max_iterations_override: int | None = None,
) -> DispatchAck:
    """Start the Temporal workflow for an existing investigation (shared by
    manual start and error intake). A failed start is never reported as
    success (F-008). `max_iterations_override` (reprocess budgets) wins over
    the profile-resolved value when positive."""
    # Lifecycle gate (§4): retired profiles block new starts (in-flight
    # workflow runs already underway are unaffected).
    profile_repo = getattr(ctx, "profile_repo", None)
    revisions: list = []
    if profile_repo is not None:
        try:
            revisions = await profile_repo.list_revisions(
                tenant_id, str(investigation.application_id)
            )
        except Exception:
            revisions = []
        if revisions and revisions[0].status.value in ("DEPRECATED", "ARCHIVED"):
            raise ApiError(
                code="PROFILE_RETIRED", http_status=status.HTTP_409_CONFLICT, message=f"Application profile is {revisions[0].status.value}; new starts are blocked"
            )

    # Persist RUNNING before dispatch so the status is real (D14), not a
    # DTO literal: UI clients get a typed member and the aggregate can never
    # sit in CREATED with a live workflow behind it. CREATED/FAILED enter
    # RUNNING; an already-RUNNING aggregate proceeds (prior attempt may have
    # dispatched already — Temporal dedupes by workflow id). Any other status
    # is not startable. Persistence precedes the Temporal call and any
    # failure here aborts before dispatch (F-008).
    if investigation.status in (InvestigationStatus.CREATED, InvestigationStatus.FAILED):
        from investigation_agent_platform.domain.common.exceptions import (
            InvalidLifecycleTransitionException,
        )

        try:
            running, _ = investigation.transition_to(
                InvestigationStatus.RUNNING, ActorType.SYSTEM, "workflow dispatch requested"
            )
        except InvalidLifecycleTransitionException as exc:
            raise ApiError(
                code="INVESTIGATION_NOT_STARTABLE", http_status=status.HTTP_409_CONFLICT, message=str(exc)
            ) from exc
        try:
            await ctx.investigation_repo.save(tenant_id, running, investigation.version)
        except ConcurrencyError as exc:
            raise ApiError(
                code="CONCURRENCY_CONFLICT", http_status=status.HTTP_409_CONFLICT, message=str(exc)
            ) from exc
        investigation = running
    elif investigation.status != InvestigationStatus.RUNNING:
        raise ApiError(
            code="INVESTIGATION_NOT_STARTABLE", http_status=status.HTTP_409_CONFLICT, message=f"Only CREATED, FAILED, or RUNNING investigations can be started (status is {investigation.status.value})"
        )
    temporal_client = getattr(ctx, "temporal_client", None)
    if temporal_client is None:
        logger.error(
            "Cannot start workflow: Temporal client is not wired",
            extra=sanitize_extra(
                {
                    "investigation_id": investigation_id,
                    "correlation_id": correlation_id,
                    "tenant_id": tenant_id,
                    "event": "workflow.dispatch.temporal_unavailable",
                }
            ),
        )
        raise ApiError(
            code="TEMPORAL_UNAVAILABLE", http_status=status.HTTP_503_SERVICE_UNAVAILABLE, message="Workflow execution engine is unavailable; investigation was not started"
        )

    # Fail loud when the worker could never load this investigation: the API
    # and the Temporal worker are separate processes sharing state only
    # through Postgres (direct SQLAlchemy repos or a BufferedProxy with a
    # live primary). Dispatching from a process-local in-memory store used
    # to reconcile the workflow straight to FAILED at
    # create_investigation_activity (InvestigationNotFoundError).
    if not isWorkerVisibleStore(getattr(ctx, "investigation_repo", None)):
        logger.error(
            "Cannot start workflow: investigation store is not worker-visible",
            extra=sanitize_extra(
                {
                    "investigation_id": investigation_id,
                    "correlation_id": correlation_id,
                    "tenant_id": tenant_id,
                    "event": "workflow.dispatch.store_not_shared",
                }
            ),
        )
        raise ApiError(
            code="STORE_NOT_SHARED", http_status=status.HTTP_503_SERVICE_UNAVAILABLE, message="Investigation state is process-local (set IAP_BUFFER_ENABLED=true and restart API and worker so both share Postgres); investigation was not started"
        )

    # Propagate correlation_id to Temporal via workflow memo/headers.
    try:
        from investigation_agent_platform.application.worker.activities import (
            overlay_evidence_budget,
        )
        from investigation_agent_platform.application.worker.workflows import RunInvestigationInput
        from investigation_agent_platform.domain.investigation.models import (
            InvestigationLimits,
        )

        # Iteration budget resolved at dispatch from the effective limits
        # (global defaults plus the profile evidence_budget overlay), so a
        # per-app budget increase takes effect without code changes or
        # restarts. Falls back to the workflow guard default when the
        # profile is unavailable.
        max_iterations: int | None = None
        try:
            if revisions:
                effective_limits = overlay_evidence_budget(
                    InvestigationLimits(), revisions[0]
                )
                max_iterations = int(effective_limits.max_iterations)
        except Exception:
            max_iterations = None
        if max_iterations_override is not None and max_iterations_override >= 1:
            max_iterations = min(int(max_iterations_override), 100)

        wf_input = RunInvestigationInput(
            application_id=str(investigation.application_id),
            session_id=getattr(investigation, "session_id", None),
            tenant_id=tenant_id,
            description=getattr(investigation, "problem_description", None),
            correlation_id=correlation_id,
            investigation_id=investigation_id,
            max_iterations=max_iterations,
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
        logger.exception(
            "Failed to start Temporal workflow",
            extra=sanitize_extra(
                {
                    "correlation_id": correlation_id,
                    "investigation_id": investigation_id,
                    "tenant_id": tenant_id,
                    "event": "workflow.dispatch.failed",
                }
            ),
        )
        raise ApiError(
            code="WORKFLOW_START_FAILED", http_status=status.HTTP_502_BAD_GATEWAY, message="Failed to start workflow execution; investigation was not started"
        ) from exc

    logger.info(
        "Dispatched Temporal workflow start for investigation=%s",
        investigation_id,
        extra=sanitize_extra({"correlation_id": correlation_id, "tenant_id": tenant_id}),
    )
    return DispatchAck(
        investigation_id=str(investigation.id),
        status="RUNNING",
        workflow_id=f"wf-investigation-{investigation_id}",
        correlation_id=correlation_id,
        message="Workflow started successfully",
    )


def _intake_service_principals() -> set[str]:
    import os

    raw = os.environ.get("IAP_INTAKE_SERVICE_PRINCIPALS", "")
    return {p.strip() for p in raw.split(",") if p.strip()}


@router.post(
    "/intake/errors",
    response_model=ErrorIntakeAck,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
    status_code=status.HTTP_202_ACCEPTED,
)
async def intake_error(
    body: ErrorIntakeBody,
    request: Request,
    x_tenant_id: str = Depends(require_tenant),
    principal_id: str = Depends(require_principal),
) -> ErrorIntakeAck:
    """Accept an auto-forwarded error, merge-or-fork by code-issue fingerprint,
    and dispatch the workflow (Part 6 D4/D8, Slice 0).

    Service-identity auth only: the principal must appear in
    `IAP_INTAKE_SERVICE_PRINCIPALS` (fail closed when unconfigured).
    """
    if principal_id not in _intake_service_principals():
        raise ApiError(
            code="INTAKE_FORBIDDEN", http_status=status.HTTP_403_FORBIDDEN, message="Intake requires an authorized service principal"
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
            raise ApiError(
                code=getattr(exc, "error_code", "DOMAIN_ERROR"), http_status=exc.http_status_code, message=exc.message
            ) from exc
        raise ApiError(
            code="INTAKE_FAILED", http_status=status.HTTP_500_INTERNAL_SERVER_ERROR, message="Intake failed"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(
        x_tenant_id, result.investigation_id
    )
    if investigation is None:  # pragma: no cover - defensive; intake just created it
        raise ApiError(
            code="INTAKE_INVESTIGATION_NOT_FOUND", http_status=status.HTTP_500_INTERNAL_SERVER_ERROR, message="Intake recorded but investigation not found"
        )
    dispatch = await _dispatch_workflow(
        ctx, investigation, x_tenant_id, str(result.investigation_id), correlation_id
    )
    return ErrorIntakeAck(
        investigation_id=dispatch.investigation_id,
        status=dispatch.status,
        workflow_id=dispatch.workflow_id,
        correlation_id=dispatch.correlation_id,
        message=dispatch.message,
        session_number=result.session_number,
        merged=result.merged,
        code_issue_fingerprint=result.code_issue_fingerprint,
    )


@router.get(
    "/investigations/{investigation_id}",
    response_model=InvestigationDetailResponse,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def get_investigation(
    investigation_id: str,
    x_tenant_id: str = Depends(require_tenant),
) -> InvestigationDetailResponse:
    """Retrieves an investigation record scoped to the authorized tenant context."""
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise ApiError(
            code="INVALID_INVESTIGATION_ID", http_status=status.HTTP_400_BAD_REQUEST, message="Invalid investigation id format"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise ApiError(
            code="INVESTIGATION_NOT_FOUND", http_status=status.HTTP_404_NOT_FOUND, message="Investigation not found"
        )

    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise ApiError(
            code="TENANT_MISMATCH", http_status=status.HTTP_403_FORBIDDEN, message="Tenant authorization mismatch"
        )

    # Part 11.5: surface pending input requirements when suspended, so
    # callers learn exactly what data to supply without a second round-trip.
    pending_requirements: list[dict[str, Any]] = []
    if investigation.status == InvestigationStatus.AWAITING_INPUT:
        input_repo = getattr(ctx, "input_repo", None)
        if input_repo is not None:
            pending = await input_repo.get_pending(x_tenant_id, investigation_uuid)
            pending_requirements = [req.model_dump(mode="json") for req in pending]

    # §2.4: surface inbound/outbound investigation-family links so operators
    # see families, not just pairs. Fail-open: link reads never break detail.
    links: list[dict[str, Any]] = []
    relationship_repo = getattr(ctx, "relationship_repo", None)
    if relationship_repo is not None and hasattr(relationship_repo, "fetch_links_for_node"):
        try:
            from investigation_agent_platform.application.investigation.disposition import (
                investigation_node,
            )

            links = await relationship_repo.fetch_links_for_node(
                x_tenant_id, investigation_node(investigation_uuid)
            )
        except Exception as exc:
            logger.warning("Investigation link read skipped: %s", exc)

    return InvestigationDetailResponse.model_validate(
        {
            "investigation": investigation.model_dump(mode="json"),
            "pending_requirements": pending_requirements,
            "links": links,
        }
    )


@router.post(
    "/investigations/{investigation_id}/input-requirements/{requirement_id}/fulfill",
    response_model=FulfillAck,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
    status_code=status.HTTP_202_ACCEPTED,
)
async def fulfill_input_requirement(
    investigation_id: str,
    requirement_id: str,
    body: FulfillInputBody,
    x_tenant_id: str = Depends(require_tenant),
    principal_id: str = Depends(require_principal),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
) -> FulfillAck:
    """Fulfill a pending input requirement via Temporal Update (Part 11.5).

    Validation split: this endpoint checks auth, tenant, JSON Schema, size,
    and the durable compare-and-set; the workflow Update validator re-checks
    id/version against in-memory state (validators cannot do I/O). Persist
    order is fulfill-row → FULFILLED → Update, so a failed Update is safely
    replayable under the same idempotency key.
    """
    import hashlib

    if not x_idempotency_key:
        raise ApiError(
            code="IDEMPOTENCY_KEY_REQUIRED", http_status=status.HTTP_400_BAD_REQUEST, message="X-Idempotency-Key header is required"
        )
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
        requirement_uuid = UUID(requirement_id)
    except ValueError as exc:
        raise ApiError(
            code="INVALID_ID_FORMAT", http_status=status.HTTP_400_BAD_REQUEST, message="Invalid id format"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise ApiError(
            code="INVESTIGATION_NOT_FOUND", http_status=status.HTTP_404_NOT_FOUND, message="Investigation not found"
        )
    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise ApiError(
            code="TENANT_MISMATCH", http_status=status.HTTP_403_FORBIDDEN, message="Tenant authorization mismatch"
        )
    if investigation.status != InvestigationStatus.AWAITING_INPUT:
        raise ApiError(
            code="INVESTIGATION_NOT_AWAITING_INPUT", http_status=status.HTTP_409_CONFLICT, message=f"Investigation is not awaiting input (status {investigation.status.value})"
        )

    canonical = json.dumps(body.data, sort_keys=True, separators=(",", ":"))
    if len(canonical.encode("utf-8")) > MAX_FULFILLMENT_BYTES:
        raise ApiError(
            code="FULFILLMENT_TOO_LARGE", http_status=status.HTTP_422_UNPROCESSABLE_ENTITY, message=f"Fulfillment payload exceeds {MAX_FULFILLMENT_BYTES} bytes"
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
        raise ApiError(
            code="IDEMPOTENCY_CONFLICT", http_status=status.HTTP_409_CONFLICT, message=str(exc)
        ) from exc
    except IdempotencyInProgressError as exc:
        raise ApiError(
            code="IDEMPOTENCY_CONFLICT", http_status=status.HTTP_409_CONFLICT, message=str(exc)
        ) from exc
    if not reserved:
        logger.info("Replaying cached fulfill-input response for key=%s", x_idempotency_key)
        return FulfillAck.model_validate(cached or {})

    input_repo = getattr(ctx, "input_repo", None)
    if input_repo is None:
        raise ApiError(
            code="INPUT_STORE_UNAVAILABLE", http_status=status.HTTP_503_SERVICE_UNAVAILABLE, message="Input requirement store is unavailable"
        )
    requirement = await input_repo.get_by_id(x_tenant_id, requirement_uuid)
    if requirement is None or requirement.investigation_id != investigation_uuid:
        raise ApiError(
            code="INPUT_REQUIREMENT_NOT_FOUND", http_status=status.HTTP_404_NOT_FOUND, message="Input requirement not found"
        )
    if requirement.state != RequirementState.PENDING:
        raise ApiError(
            code="REQUIREMENT_NOT_PENDING", http_status=status.HTTP_409_CONFLICT, message=f"Input requirement is {requirement.state.value}, not PENDING"
        )
    if requirement.requirement_version != body.requirement_version:
        raise ApiError(
            code="STALE_REQUIREMENT_VERSION", http_status=status.HTTP_409_CONFLICT, message="Stale requirement version; reload pending requirements"
        )
    if requirement.json_schema:
        import jsonschema  # type: ignore[import-untyped]

        try:
            jsonschema.validate(body.data, requirement.json_schema)
        except Exception as exc:
            raise ApiError(
                code="FULFILLMENT_SCHEMA_INVALID", http_status=status.HTTP_422_UNPROCESSABLE_ENTITY, message=f"Fulfillment data failed requirement schema: {exc}"
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
        raise ApiError(
            code="CONCURRENCY_CONFLICT", http_status=status.HTTP_409_CONFLICT, message=str(exc)
        ) from exc

    temporal_client = getattr(ctx, "temporal_client", None)
    if temporal_client is None:
        raise ApiError(
            code="TEMPORAL_UNAVAILABLE", http_status=status.HTTP_503_SERVICE_UNAVAILABLE, message="Workflow execution engine is unavailable; cannot deliver input"
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
            raise ApiError(
                code="FULFILL_DELIVERY_CONFLICT", http_status=status.HTTP_409_CONFLICT, message=message
            ) from exc
        logger.exception(
            "Failed to deliver fulfill_input update",
            extra=sanitize_extra(
                {
                    "investigation_id": investigation_id,
                    "tenant_id": x_tenant_id,
                    "event": "fulfill_input.delivery_failed",
                }
            ),
        )
        raise ApiError(
            code="FULFILL_DELIVERY_FAILED", http_status=status.HTTP_502_BAD_GATEWAY, message="Failed to deliver input to workflow"
        ) from exc

    response_payload = FulfillAck(
        investigation_id=investigation_id,
        requirement_id=requirement_id,
        status="FULFILLED",
        update=update_result if isinstance(update_result, dict) else {},
    )
    await ctx.idempotency_store.complete(
        x_tenant_id,
        x_idempotency_key,
        response_payload.model_dump(mode="json", exclude_unset=True),
        operation="input-fulfill",
        application_id=investigation.application_id,
    )
    return response_payload


@router.get(
    "/investigations",
    response_model=ListEnvelope[InvestigationListItemView],
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def list_investigations(
    x_tenant_id: str = Depends(require_tenant),
    limit: int = Query(default=50, ge=1, le=200),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        max_length=256,
        description="Comma-separated status filter (e.g. COMPLETED,FAILED). "
        "Omit for open investigations only.",
    ),
    application_id: str | None = Query(
        default=None, min_length=1, max_length=128,
        description="Restrict to one application profile id.",
    ),
) -> ListEnvelope[InvestigationListItemView]:
    """Lists investigations for the tenant (most recent first, best-effort order).

    Without filters this returns open investigations only (backed by
    `list_open_ids`); pass `status` and/or `application_id` to include
    terminal investigations and scope by application.
    """
    ctx = get_app_context()
    statuses: list[str] | None = None
    if status_filter is not None:
        try:
            statuses = [
                InvestigationStatus(part.strip().upper()).value
                for part in status_filter.split(",")
                if part.strip()
            ]
        except ValueError as exc:
            raise ApiError(
                code="INVALID_STATUS_FILTER", http_status=status.HTTP_400_BAD_REQUEST, message=f"Unknown status in filter: {status_filter!r}"
            ) from exc
        if not statuses:
            raise ApiError(
                code="INVALID_STATUS_FILTER", http_status=status.HTTP_400_BAD_REQUEST, message=f"Unknown status in filter: {status_filter!r}"
            )
    if statuses is None and application_id is None:
        ids = await ctx.investigation_repo.list_open_ids(x_tenant_id)
    else:
        ids = await ctx.investigation_repo.list_filtered(
            x_tenant_id, statuses, application_id, limit
        )
    items = []
    for inv_id in ids[:limit]:
        inv = await ctx.get_investigation_service().execute(x_tenant_id, inv_id)
        if inv is None or getattr(inv, "tenant_id", x_tenant_id) != x_tenant_id:
            continue
        items.append(
            InvestigationListItemView(
                investigation_id=str(inv.id),
                status=inv.status.value,
                phase=inv.phase,
                application_id=inv.application_id,
                created_at=inv.created_at.isoformat() if inv.created_at else None,
                updated_at=inv.updated_at.isoformat() if inv.updated_at else None,
            )
        )
    return ListEnvelope[InvestigationListItemView](items=items, total=len(items), offset=0, limit=limit)


@router.get(
    "/investigations/{investigation_id}/conclusion",
    response_model=ConclusionResponse,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def get_conclusion(
    investigation_id: str,
    x_tenant_id: str = Depends(require_tenant),
) -> ConclusionResponse:
    """Fetches the terminal root-cause conclusion; null while unconcluded."""
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise ApiError(
            code="INVALID_INVESTIGATION_ID", http_status=status.HTTP_400_BAD_REQUEST, message="Invalid investigation id format"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise ApiError(
            code="INVESTIGATION_NOT_FOUND", http_status=status.HTTP_404_NOT_FOUND, message="Investigation not found"
        )

    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise ApiError(
            code="TENANT_MISMATCH", http_status=status.HTTP_403_FORBIDDEN, message="Tenant authorization mismatch"
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
    failure = (getattr(investigation, "metadata", None) or {}).get("failure")
    return ConclusionResponse.model_validate(
        {
            "investigation_id": investigation_id,
            "status": investigation.status.value,
            "conclusion": conclusion.model_dump(mode="json") if conclusion is not None else None,
            "failure": failure,
        }
    )


@router.get(
    "/investigations/{investigation_id}/findings",
    response_model=ListEnvelope[Finding],
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def list_findings(
    investigation_id: str,
    x_tenant_id: str = Depends(require_tenant),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> ListEnvelope[Finding]:
    """Retrieves paginated findings for an investigation (Part 11.2)."""
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise ApiError(
            code="INVALID_INVESTIGATION_ID", http_status=status.HTTP_400_BAD_REQUEST, message="Invalid investigation id format"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise ApiError(
            code="INVESTIGATION_NOT_FOUND", http_status=status.HTTP_404_NOT_FOUND, message="Investigation not found"
        )
    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise ApiError(
            code="TENANT_MISMATCH", http_status=status.HTTP_403_FORBIDDEN, message="Tenant authorization mismatch"
        )

    finding_repo = getattr(ctx, "finding_repo", None)
    if finding_repo is None:
        return ListEnvelope[Finding](items=[], total=0, offset=offset, limit=limit)
    findings, total = await finding_repo.list_findings(
        x_tenant_id, investigation_uuid, limit=limit, offset=offset
    )
    return ListEnvelope[Finding].model_validate(
        {
            "items": [f.model_dump(mode="json") for f in findings],
            "total": total,
            "offset": offset,
            "limit": limit,
        }
    )


@router.post(
    "/investigations/{investigation_id}/retry",
    response_model=DispatchAck,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_investigation(
    investigation_id: str,
    request: Request,
    x_tenant_id: str = Depends(require_tenant),
) -> DispatchAck:
    """Redispatches the Temporal workflow for a FAILED investigation."""
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise ApiError(
            code="INVALID_INVESTIGATION_ID", http_status=status.HTTP_400_BAD_REQUEST, message="Invalid investigation id format"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise ApiError(
            code="INVESTIGATION_NOT_FOUND", http_status=status.HTTP_404_NOT_FOUND, message="Investigation not found"
        )

    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise ApiError(
            code="TENANT_MISMATCH", http_status=status.HTTP_403_FORBIDDEN, message="Tenant authorization mismatch"
        )
    if investigation.status != InvestigationStatus.FAILED:
        raise ApiError(
            code="INVESTIGATION_NOT_RETRYABLE", http_status=status.HTTP_409_CONFLICT, message=f"Only FAILED investigations can be retried (status is {investigation.status.value})"
        )

    correlation_id = getattr(request.state, "correlation_id", None) or request.headers.get(
        "X-Correlation-ID"
    )
    return await _dispatch_workflow(
        ctx, investigation, x_tenant_id, investigation_id, correlation_id
    )


@router.post(
    "/investigations/{investigation_id}/reprocess",
    response_model=DispatchAck,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reprocess_investigation(
    investigation_id: str,
    body: ReprocessBody,
    request: Request,
    x_tenant_id: str = Depends(require_tenant),
    principal_id: str = Depends(require_principal),
) -> DispatchAck:
    """Version-per-reprocess for terminal investigations (FRESH or CONTINUE).

    Always creates a NEW investigation version — terminal rows are never
    mutated. Disposition matching/linking is bypassed by construction: a
    restart is an explicit operator action, not a triaged request, so no
    duplicate/related links are written (lineage travels in metadata only).
    RESTART_CONTINUE on an active investigation is rejected (redispatch the
    live run instead). Budget multiplier applies here only, with tokens
    hard-capped at 1M per run and the run ledger reset.
    """
    from investigation_agent_platform.application.investigation.services import (
        BudgetApprovalDeniedException,
        ReprocessInvestigationService,
    )
    from investigation_agent_platform.domain.common.exceptions import DomainException

    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise ApiError(
            code="INVALID_INVESTIGATION_ID", http_status=status.HTTP_400_BAD_REQUEST, message="Invalid investigation id format"
        ) from exc

    service = ReprocessInvestigationService(
        investigation_repo=ctx.investigation_repo,
        profile_repo=ctx.profile_repo,
        evidence_repo=getattr(ctx, "evidence_repo", None),
        hypothesis_repo=getattr(ctx, "hypothesis_repo", None),
        finding_repo=getattr(ctx, "finding_repo", None),
        checkpoint_repo=getattr(ctx, "checkpoint_repo", None),
        create_service=ctx.create_investigation_service(),
    )
    correlation_id = getattr(request.state, "correlation_id", None) or request.headers.get(
        "X-Correlation-ID"
    )
    try:
        new_investigation = await service.execute(
            tenant_id=x_tenant_id,
            investigation_id=investigation_uuid,
            mode=body.mode,
            budget_multiplier=body.budget_multiplier,
            principal_id=principal_id,
            reason=body.reason,
        )
    except BudgetApprovalDeniedException as exc:
        raise ApiError(
            code="BUDGET_APPROVAL_DENIED", http_status=status.HTTP_403_FORBIDDEN, message=str(exc)
        ) from exc
    except DomainException as exc:
        raise ApiError(
            code=getattr(exc, "error_code", "DOMAIN_ERROR"), http_status=exc.http_status_code, message=exc.message
        ) from exc

    effective = (new_investigation.metadata or {}).get("effective_limits") or {}
    max_iterations_override = effective.get("max_iterations")
    if not isinstance(max_iterations_override, int):
        max_iterations_override = None
    return await _dispatch_workflow(
        ctx,
        new_investigation,
        x_tenant_id,
        str(new_investigation.id),
        correlation_id,
        max_iterations_override=max_iterations_override,
    )


@router.post(
    "/investigations/{investigation_id}/cancel",    response_model=CancelAck,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_investigation(
    investigation_id: str,
    reason: str = Query(
        default="API cancellation requested", max_length=1000, pattern=NO_NUL_PATTERN
    ),
    x_tenant_id: str = Depends(require_tenant),
) -> CancelAck:
    """Issues cancellation signal to Temporal workflow execution and updates persistence status."""
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise ApiError(
            code="INVALID_INVESTIGATION_ID", http_status=status.HTTP_400_BAD_REQUEST, message="Invalid investigation id format"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise ApiError(
            code="INVESTIGATION_NOT_FOUND", http_status=status.HTTP_404_NOT_FOUND, message="Investigation not found"
        )

    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise ApiError(
            code="TENANT_MISMATCH", http_status=status.HTTP_403_FORBIDDEN, message="Tenant authorization mismatch"
        )

    await ctx.cancel_investigation_service().execute(x_tenant_id, investigation_uuid, reason=reason)
    return CancelAck(investigation_id=investigation_id, status="CANCELLING", reason=reason)
