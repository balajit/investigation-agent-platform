# src/investigation_agent_platform/application/worker/activities.py
"""Temporal activity thin adapters — each delegates to the application/domain layer via AppContext."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import deque
from dataclasses import dataclass, field
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

from investigation_agent_platform.domain.investigation.models import ActionType

# Action types that must reach the evidence gateway to execute; used to
# fail closed (EVIDENCE_PROVIDER_UNAVAILABLE) rather than silently no-op when
# no gateway is wired (F-021/F-023).
_ACTION_REQUIRED_EVIDENCE_TYPE_LOCAL = {
    ActionType.QUERY_STATE,
    ActionType.SEARCH_LOGS,
    ActionType.GET_CODE,
    ActionType.CORRELATE,
}


def _application_failure_from_exc(exc: BaseException) -> ApplicationFailure:
    """Map exceptions to Temporal ApplicationFailure using an explicit retry taxonomy (F-056).

    - ``DomainException`` carries its own explicit ``retryable`` flag — honored as-is.
    - Known transient infrastructure errors (timeouts, connection errors) retry.
    - Known non-transient programming/validation errors (bad input, bugs) never retry.
    - Any other unclassified exception defaults to NON-retryable: an unknown
      failure mode must not be assumed safe to blindly retry and burn budget.
    """
    import asyncio as _asyncio

    from investigation_agent_platform.domain.common.exceptions import DomainException

    if isinstance(exc, DomainException):
        return ApplicationFailure(
            str(exc),
            type=exc.__class__.__name__,
            non_retryable=not exc.retryable,
        )

    _TRANSIENT_INFRA_TYPES = (
        ConnectionError,
        TimeoutError,
        _asyncio.TimeoutError,
        OSError,
    )
    _NON_RETRYABLE_TYPES = (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        LookupError,
        AssertionError,
    )

    if isinstance(exc, _TRANSIENT_INFRA_TYPES):
        return ApplicationFailure(str(exc), type=exc.__class__.__name__, non_retryable=False)
    if isinstance(exc, _NON_RETRYABLE_TYPES):
        return ApplicationFailure(str(exc), type=exc.__class__.__name__, non_retryable=True)
    # Unclassified exception: fail closed rather than retry indefinitely.
    return ApplicationFailure(str(exc), type=exc.__class__.__name__, non_retryable=True)


# ---------------------------------------------------------------------------
# Wave 2 shared helpers (TO_DO-007/011/012/013/016): telemetry extras,
# strict input coercion, idempotency wiring, actor threading, domain graph.
# ---------------------------------------------------------------------------


def _sanitize(extra: dict[str, Any]) -> dict[str, Any]:
    """Apply the api/errors.py redaction policy to a log ``extra`` dict (TO_DO-009).

    Function-level import with cycle justification (C13): importing
    ``investigation_agent_platform.api.errors`` at module top executes the
    ``api`` package ``__init__`` (FastAPI app wiring); this module already
    keeps ``api`` imports lazy (see ``_get_ctx``) to avoid a worker->api
    import cycle.     ``sys.modules`` caching makes repeat calls cheap.
    """
    # Function-level: top-level import would execute api/__init__ (FastAPI app) -> cycle (C13).
    from investigation_agent_platform.api.errors import sanitize_extra

    return sanitize_extra(extra)


def _log_extra(
    *,
    tenant_id: str = "",
    investigation_id: Any = "",
    event: str = "",
    error: str | None = None,
    error_type: str | None = None,
    **data: Any,
) -> dict[str, Any]:
    """Build a C3 telemetry ``extra`` dict: IDs plus stable ``event`` (TO_DO-007).

    ``investigation_id`` accepts ``UUID``/``None`` and is stringified. Every
    value passes through ``sanitize_extra`` so secret-bearing keys can never
    leak outside the API layer (TO_DO-009).
    """
    extra: dict[str, Any] = {
        "tenant_id": tenant_id or "",
        "investigation_id": "" if investigation_id is None else str(investigation_id),
        "event": event,
    }
    if error is not None:
        extra["error"] = error
    if error_type is not None:
        extra["error_type"] = error_type
    extra.update(data)
    return _sanitize(extra)


def _coerced_data(cls: type, params: object, *, activity: str) -> dict[str, Any]:
    """Strict activity-input coercion replacing ``X | dict[str, Any]`` unions (TO_DO-016).

    Temporal may deliver inputs as plain dicts; both typed dataclass
    instances and dicts are accepted, but dicts are validated field-by-field:
    every required dataclass field must be present as a key. Violations raise
    non-retryable ``ApplicationFailure`` instead of surfacing later as
    ``KeyError``/``ValueError`` deep in workflow logic. Present-but-empty
    values still flow to the existing per-activity guards, which return the
    ``success=False`` envelope (backward compatible with validation-style
    tests). (Inputs stay stdlib dataclasses — not Pydantic ``BaseModel`` —
    because the Temporal payload converter in this repo is dataclass-based;
    this helper is the functional equivalent of ``Model.model_validate`` at
    the boundary.)
    """
    import dataclasses as _dataclasses

    if isinstance(params, cls):
        return {f.name: getattr(params, f.name) for f in _dataclasses.fields(cls)}
    if isinstance(params, dict):
        fields = _dataclasses.fields(cls)
        known = {f.name for f in fields}
        problems = sorted(
            f.name
            for f in fields
            if f.default is _dataclasses.MISSING
            and f.default_factory is _dataclasses.MISSING  # type: ignore[comparison-overlap]
            and f.name not in params
        )
        if problems:
            logger.warning(
                "%s rejected invalid activity input: %s",
                activity,
                ", ".join(problems),
                extra=_log_extra(event=f"{activity} input rejected"),
            )
            raise ApplicationFailure(
                f"{activity} received invalid input: missing {', '.join(problems)}",
                type="ActivityInputValidationError",
                non_retryable=True,
            )
        try:
            instance = cls(**{k: v for k, v in params.items() if k in known})
        except TypeError as exc:
            logger.exception(
                "%s rejected malformed activity input",
                activity,
                extra=_log_extra(event=f"{activity} input rejected", error=str(exc)),
            )
            raise ApplicationFailure(
                f"{activity} received invalid input: {exc}",
                type="ActivityInputValidationError",
                non_retryable=True,
            ) from exc
        return {f.name: getattr(instance, f.name) for f in fields}
    logger.warning(
        "%s rejected unsupported input type %s",
        activity,
        type(params).__name__,
        extra=_log_extra(event=f"{activity} input rejected"),
    )
    raise ApplicationFailure(
        f"{activity} received unsupported input of type {type(params).__name__}; "
        f"expected {cls.__name__} or dict",
        type="ActivityInputValidationError",
        non_retryable=True,
    )


def _idempotency_store_for(ctx: Any) -> Any | None:
    """Return the ctx idempotency store when it speaks the durable protocol.

    ``Any`` is justified: ``AppContext`` is assembled in bootstrap/api layers
    and intentionally untyped at the worker boundary; the structural check
    keeps activities backward compatible with minimal/test contexts that
    carry no store (idempotency then degrades to downstream dedup, logged).
    """
    store = getattr(ctx, "idempotency_store", None)
    if store is None:
        return None
    if not (hasattr(store, "reserve_or_get") and hasattr(store, "complete")):
        return None
    return store


def _request_hash(payload: dict[str, Any]) -> str:
    """Deterministic F-014 request hash for an idempotency reservation."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _reserve_or_replay(
    *,
    ctx: Any,
    tenant_id: str,
    key: str,
    request_payload: dict[str, Any],
    operation: str,
    activity: str,
    investigation_id: Any = "",
) -> tuple[dict[str, Any] | None, str | None]:
    """Reserve an idempotency key or return the stored replay response (TO_DO-011).

    Returns ``(replayed_response, active_key)``: when ``replayed_response`` is
    not ``None`` the caller must return it without re-executing. When the
    store is unwired, returns ``(None, None)`` and the caller proceeds —
    downstream dedup (deterministic ``action_id`` F-057, evidence fingerprint,
    outbox key) remains the safety net, and the skip is logged loudly.
    """
    store = _idempotency_store_for(ctx)
    if store is None:
        logger.warning(
            "%s proceeding without idempotency reservation; store unwired",
            activity,
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                event=f"{activity} idempotency skipped",
                idempotency_key=key,
            ),
        )
        return None, None
    # Function-level: keeps the worker import graph light (C13).
    from investigation_agent_platform.domain.common.exceptions import (
        IdempotencyConflictError,
        IdempotencyInProgressError,
    )

    try:
        cached, reserved = await store.reserve_or_get(
            tenant_id, key, _request_hash(request_payload), operation=operation
        )
    except IdempotencyConflictError as exc:
        logger.warning(
            "%s idempotency conflict; refusing duplicate",
            activity,
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                event=f"{activity} idempotency conflict",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise ApplicationFailure(
            str(exc), type="IdempotencyKeyConflict", non_retryable=True
        ) from exc
    except IdempotencyInProgressError as exc:
        logger.warning(
            "%s idempotency already in progress; yielding to owner",
            activity,
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                event=f"{activity} idempotency in progress",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise ApplicationFailure(
            str(exc), type="IdempotencyRequestInProgress", non_retryable=False
        ) from exc
    if not reserved:
        logger.info(
            "%s idempotency replay; skipping re-execution",
            activity,
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                event=f"{activity} idempotency replay",
                idempotency_key=key,
            ),
        )
        return (cached or {}), None
    return None, key


async def _complete_reservation(
    *,
    ctx: Any,
    tenant_id: str,
    key: str,
    operation: str,
    response: dict[str, Any],
    activity: str,
    investigation_id: Any = "",
) -> None:
    """Store an idempotency response; failures are loud but never fail the activity.

    The mutation already succeeded at this point: a pending reservation only
    means a later retry re-executes (downstream dedup still applies), so a
    ``complete`` failure must not convert success into failure.
    """
    store = _idempotency_store_for(ctx)
    if store is None:
        return
    try:
        await store.complete(tenant_id, key, response, operation=operation)
    except Exception as exc:
        logger.exception(
            "%s idempotency complete failed; reservation left pending",
            activity,
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                event=f"{activity} idempotency complete failed",
                error=str(exc),
                error_type=type(exc).__name__,
                idempotency_key=key,
            ),
        )


async def _abandon_reservation(
    *,
    ctx: Any,
    tenant_id: str,
    key: str | None,
    operation: str,
    activity: str,
    investigation_id: Any = "",
) -> None:
    """Release a pending reservation after the reserved body raised.

    Without this, a failed attempt leaves an in-progress lease that turns
    every Temporal retry into IdempotencyRequestInProgress until TTL expiry.
    Safe to call when no lease is active (None/empty key or operation) and
    when the store is unwired or lacks ``abandon``; failures are loud but
    never mask the original error.
    """
    if not key or not operation:
        return
    store = _idempotency_store_for(ctx)
    if store is None:
        return
    abandon = getattr(store, "abandon", None)
    if not callable(abandon):
        return
    try:
        await abandon(tenant_id, key, operation)
    except Exception as exc:
        logger.exception(
            "%s idempotency abandon failed; retry may see a stale lease",
            activity,
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                event=f"{activity} idempotency abandon failed",
                error=str(exc),
                error_type=type(exc).__name__,
                idempotency_key=key,
            ),
        )


_DOMAIN_TRANSITION_GRAPH: dict[Any, set[Any]] | None = None
# Any-keyed: statuses are domain InvestigationStatus enums resolved lazily to
# honor this module's no-top-level-domain-import convention beyond ActionType.


def _domain_transition_graph() -> dict[Any, set[Any]]:
    """Derive lifecycle adjacency from the domain single source (TO_DO-013).

    Probes ``Investigation.transition_to`` on scratch instances instead of
    duplicating the edge table: any future domain edge edit is picked up
    without a worker change. Computed once per process.
    """
    global _DOMAIN_TRANSITION_GRAPH
    if _DOMAIN_TRANSITION_GRAPH is not None:
        return _DOMAIN_TRANSITION_GRAPH
    # Function-level imports: keeps the worker import graph light (C13).
    from investigation_agent_platform.domain.common.exceptions import (
        InvalidLifecycleTransitionException,
    )
    from investigation_agent_platform.domain.investigation.models import (
        ActorType,
        Investigation,
        InvestigationStatus,
    )

    graph: dict[Any, set[Any]] = {}
    for source in InvestigationStatus:
        probe = Investigation.model_construct(status=source, version=1, started_at=None)
        reachable: set[Any] = set()
        for target in InvestigationStatus:
            try:
                probe.transition_to(target, ActorType.SYSTEM, "graph probe")
            except InvalidLifecycleTransitionException:
                continue
            reachable.add(target)
        graph[source] = reachable
    _DOMAIN_TRANSITION_GRAPH = graph
    return graph


def _normalize_actor(value: Any) -> Any:
    """Coerce an actor value to domain ActorType, defaulting to SYSTEM (TO_DO-012)."""
    # Function-level: keeps the worker import graph light (C13).
    from investigation_agent_platform.domain.investigation.models import (
        ActorType,
    )

    if isinstance(value, ActorType):
        return value
    try:
        return ActorType(str(value or "SYSTEM"))
    except ValueError:
        return ActorType.SYSTEM


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


@dataclass
class RetrieveEvidenceInput:
    tenant_id: str
    investigation_id: str
    application_id: str | None = None


@dataclass
class VerifyRootCauseInput:
    tenant_id: str
    investigation_id: str


@dataclass
class CheckpointInput:
    tenant_id: str
    investigation_id: str
    step: int = 0
    state_snapshot: dict[str, Any] | None = None


@dataclass
class ConcludeInvestigationInput:
    tenant_id: str
    investigation_id: str
    status: str


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


@dataclass
class CaptureKnowledgeInput:
    tenant_id: str
    investigation_id: str


def _get_ctx() -> Any:
    # Any justified: AppContext is assembled in bootstrap/api layers and
    # intentionally untyped at the worker boundary (see _idempotency_store_for).
    from investigation_agent_platform.api.dependencies import get_app_context

    return get_app_context()


@activity.defn
async def create_investigation_activity(
    params: CreateInvestigationInput,
) -> CreateInvestigationOutput:
    # TO_DO-011: not idempotency-wired — creation mints a new identity per
    # call so no deterministic key exists; the F-IDENTITY load path
    # (investigation_id set) is idempotent by lookup instead.
    try:
        data = _coerced_data(
            CreateInvestigationInput, params, activity="create_investigation_activity"
        )
        params = CreateInvestigationInput(**data)
        logger.info(
            "create_investigation_activity started",
            extra=_log_extra(
                tenant_id=params.tenant_id,
                investigation_id=params.investigation_id or "",
                event="create_investigation_activity started",
                application_id=params.application_id,
            ),
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
            logger.info(
                "create_investigation_activity completed",
                extra=_log_extra(
                    tenant_id=params.tenant_id,
                    investigation_id=str(existing.id),
                    event="create_investigation_activity completed",
                    outcome="loaded_existing",
                ),
            )
            return CreateInvestigationOutput(
                investigation_id=str(existing.id), status=existing.status.value
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
        logger.info(
            "create_investigation_activity completed",
            extra=_log_extra(
                tenant_id=params.tenant_id,
                investigation_id=str(inv.id),
                event="create_investigation_activity completed",
                outcome="created",
            ),
        )
        return CreateInvestigationOutput(investigation_id=str(inv.id), status=inv.status.value)
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "create_investigation_activity failed",
            extra=_log_extra(
                tenant_id=getattr(params, "tenant_id", "") if isinstance(params, CreateInvestigationInput) else "",
                investigation_id=getattr(params, "investigation_id", "") if isinstance(params, CreateInvestigationInput) else "",
                event="create_investigation_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def retrieve_evidence_activity(
    params: RetrieveEvidenceInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — this is a read path whose
    # side-effect writes dedupe on evidence fingerprint, and the workflow
    # loop intentionally re-queries every iteration expecting fresh results;
    # an activity-level replay key would freeze a point-in-time count.
    data = _coerced_data(
        RetrieveEvidenceInput, params, activity="retrieve_evidence_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "retrieve_evidence_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="retrieve_evidence_activity started",
        ),
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
            logger.exception(
                "retrieve_evidence_activity gateway call failed",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="retrieve_evidence_activity gateway call failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
            )
            raise _application_failure_from_exc(exc) from exc
        logger.info(
            "retrieve_evidence_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="retrieve_evidence_activity completed",
                outcome="retrieved",
                items_count=len(result.items),
                total_count=result.total_count,
            ),
        )
        return GenericActivityResult(
            success=True,
            data={"items_count": len(result.items), "total_count": result.total_count},
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "retrieve_evidence_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="retrieve_evidence_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def reason_activity(params: ReasonInput) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — pure compute over persisted state,
    # no durable writes of its own; re-execution is safe by construction.
    data = _coerced_data(ReasonInput, params, activity="reason_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "reason_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="reason_activity started",
            iteration=int(data.get("iteration", 0)),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        iteration = int(data.get("iteration", 0))
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
                logger.exception(
                    "reason_activity: failed to construct reasoning coordinator; "
                    "refusing to process prompts with a permissive fallback",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="reason_activity coordinator init failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
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
                logger.warning(
                    "reason profile attach failed",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="reason profile attach failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
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
                    logger.warning(
                        "reason_activity checkpoint hydration failed",
                        extra=_log_extra(
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event="reason_activity checkpoint hydration failed",
                            error=str(exc),
                            error_type=type(exc).__name__,
                        ),
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
                evidence_repo: Any = getattr(ctx, "evidence_repo", None)
                if evidence_repo is not None:
                    items = await evidence_repo.find_by_investigation_id(
                        tenant_id, investigation_id
                    )
                    state = state.model_copy(update={"evidence": list(items)[:100]})
                hypothesis_repo: Any = getattr(ctx, "hypothesis_repo", None)
                if hypothesis_repo is not None:
                    hyps = await hypothesis_repo.find_by_investigation_id(
                        tenant_id, investigation_id
                    )
                    state = state.model_copy(update={"active_hypotheses": list(hyps)[:50]})
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
                logger.warning(
                    "reason_activity state enrichment failed; reasoning unassisted",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="reason_activity state enrichment failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
                )

            decision = await coordinator.reason(tenant_id, state)
            # Choreography: the workflow dispatches on next_action /
            # action_parameters — they must be propagated here, not dropped.
            # (Previously only readiness/observations were returned, so the
            # workflow always fell back to QUERY_STATE and reasoning was
            # effectively severed from execution.)
            action = getattr(decision, "action", None)
            action_type_value = getattr(
                getattr(action, "action_type", None), "value", None
            )
            hypotheses_out = list(
                getattr(decision, "hypotheses_evaluated", None) or []
            )
            # Operational telemetry: type names and counts only, never prompt
            # or payload values (prompt content stays out of logs). Values ride
            # as extra keys (TO_DO-007/019); the message keeps lazy %-style.
            logger.warning(
                "reason_activity decision action=%s readiness=%s hypotheses=%s observations=%s",
                action_type_value or "QUERY_STATE",
                decision.conclusion_readiness,
                len(hypotheses_out),
                len(decision.observations or []),
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="reason_activity decision",
                    action=action_type_value or "QUERY_STATE",
                    readiness=decision.conclusion_readiness,
                    hypotheses_count=len(hypotheses_out),
                    observations_count=len(decision.observations or []),
                ),
            )
            logger.info(
                "reason_activity completed",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="reason_activity completed",
                    outcome="reasoned",
                    iteration=iteration,
                ),
            )
            return GenericActivityResult(
                success=True,
                data={
                    "conclusion_readiness": decision.conclusion_readiness,
                    "observations": decision.observations,
                    "iteration": iteration,
                    "next_action": action_type_value or "QUERY_STATE",
                    "action_parameters": dict(
                        getattr(action, "parameters", None) or {}
                    ),
                    "hypotheses_evaluated": hypotheses_out,
                },
            )
        except Exception as exc:
            logger.exception(
                "reason_activity reasoning call failed",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="reason_activity reasoning call failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
            )
            raise _application_failure_from_exc(exc) from exc
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "reason_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="reason_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


_BUDGET_FIELDS = (
    "max_tool_calls",
    "max_evidence_items",
    "max_hypotheses",
    "max_correlation_depth",
    "max_source_files",
    "max_database_rows",
    "max_log_query_window_minutes",
    "max_duration_seconds",
)


def overlay_evidence_budget(limits: Any, profile: Any) -> Any:
    """Overlay a profile's evidence_budget onto base limits (§7.6).

    Only known fields with positive-int values apply; anything else is
    ignored (fail-closed to the global default). Never raises.
    """
    try:
        budget = profile.investigation.evidence_budget or {}
        updates = {
            field: budget[field]
            for field in _BUDGET_FIELDS
            if isinstance(budget.get(field), int) and budget[field] >= 1
        }
        if updates:
            return limits.model_copy(update=updates)
    except Exception as exc:
        logger.exception(
            "evidence_budget overlay failed; using global limits",
            extra=_log_extra(
                event="evidence_budget overlay failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
    return limits


@activity.defn
async def execute_action_activity(
    params: ExecuteActionInput,
) -> GenericActivityResult:
    data = _coerced_data(ExecuteActionInput, params, activity="execute_action_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "execute_action_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="execute_action_activity started",
            action=str(data.get("action", "QUERY_STATE")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        action_type = str(data.get("action", "QUERY_STATE"))
        parameters: dict[str, Any] = dict(data.get("parameters", {}))
        # Reason-proposed hypothesis strings for FORMULATE_HYPOTHESIS.
        proposed_hypotheses: list[str] = [
            str(h) for h in (data.get("hypotheses", []) or []) if str(h).strip()
        ]
        if not tenant_id or not inv_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and investigation_id required"
            )
        investigation_id = UUID(inv_id_str)
        ctx = _get_ctx()

        # Mandatory authorization gate (F-004): no action reaches the evidence
        # gateway or execution surface without an explicit allow decision.
        from investigation_agent_platform.domain.investigation.models import (
            ActionType,
            InvestigationAction,
        )

        try:
            resolved_action_type = ActionType(action_type)
        except ValueError as exc:
            raise ApplicationFailure(
                f"Unknown action type: {action_type}",
                type="InvalidActionTypeError",
                non_retryable=True,
            ) from exc

        inv_repo: Any = getattr(ctx, "investigation_repo", None)
        investigation_obj: Any = None
        if inv_repo is not None:
            investigation_obj = await inv_repo.get_by_id(tenant_id, investigation_id)
        application_id = str(
            data.get("application_id")
            or (getattr(investigation_obj, "application_id", None) if investigation_obj else None)
            or ""
        )

        inv_action = InvestigationAction.create(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            action_type=resolved_action_type,
            parameters=parameters,
        )

        # TO_DO-011: idempotency-wired — action_id is deterministic (UUIDv5
        # over the execution hash, F-057), so a Temporal retry of the same
        # logical action reserves the same key instead of double-recording.
        _idem_active_key: str | None = None
        _idem_operation = "activity:execute_action"
        _idem_key = f"execute:{inv_action.action_id}"
        _idem_request = {
            "action_id": str(inv_action.action_id),
            "tenant_id": tenant_id,
            "investigation_id": inv_id_str,
            "action_type": resolved_action_type.value,
            "parameters": parameters,
        }
        _replayed, _idem_active_key = await _reserve_or_replay(
            ctx=ctx,
            tenant_id=tenant_id,
            key=_idem_key,
            request_payload=_idem_request,
            operation=_idem_operation,
            activity="execute_action_activity",
            investigation_id=inv_id_str,
        )
        if _replayed is not None:
            return GenericActivityResult(
                success=bool(_replayed.get("success", True)),
                data=dict(_replayed.get("data") or {}),
                error=_replayed.get("error"),
            )

        action_service = ctx.execute_action_service()
        principal_id = str(data.get("principal_id", "worker"))

        # F-054: durable budget ledger — usage is derived from durable tables
        # (action_executions count + evidence count), reserved/checked before
        # execution rather than advisory config. Deny before authorize so a
        # budget-exhausted investigation cannot consume further provider calls.
        from investigation_agent_platform.domain.common.exceptions import (
            InvestigationLimitExceededException,
        )
        from investigation_agent_platform.domain.investigation.models import InvestigationLimits

        limits = InvestigationLimits()
        # Per-app evidence budget (§7.6): profile evidence_budget overlays the
        # process-global defaults; unknown/invalid keys are ignored, never
        # trusted. Also resolves the profile once for the correlate clamp.
        _budget_profile: Any = None
        profile_repo_for_limits: Any = getattr(ctx, "profile_repo", None)
        if profile_repo_for_limits is not None and application_id:
            try:
                _budget_profile = await profile_repo_for_limits.get_by_application_id(
                    tenant_id, application_id
                )
            except Exception as exc:
                logger.exception(
                    "profile budget lookup failed; using global limits",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="profile budget lookup failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
                )
                _budget_profile = None
        if _budget_profile is not None:
            limits = overlay_evidence_budget(limits, _budget_profile)
        app_correlation_depth = (
            _budget_profile.investigation.correlation_depth
            if _budget_profile is not None
            else limits.max_correlation_depth
        )
        try:
            action_repo_for_budget: Any = getattr(ctx, "action_repo", None)
            evidence_repo_for_budget: Any = getattr(ctx, "evidence_repo", None)
            tool_calls = 0
            evidence_count = 0
            if action_repo_for_budget is not None and hasattr(
                action_repo_for_budget, "count_by_investigation"
            ):
                tool_calls = await action_repo_for_budget.count_by_investigation(
                    tenant_id, investigation_id
                )
            if evidence_repo_for_budget is not None:
                items = await evidence_repo_for_budget.find_by_investigation_id(
                    tenant_id, investigation_id
                )
                evidence_count = len(items)
            if tool_calls >= limits.max_tool_calls:
                raise InvestigationLimitExceededException(
                    f"Maximum tool call limit reached: {limits.max_tool_calls}"
                )
            if evidence_count >= limits.max_evidence_items:
                raise InvestigationLimitExceededException(
                    f"Maximum evidence limit reached: {limits.max_evidence_items}"
                )
        except InvestigationLimitExceededException as exc:
            # Budget exhaustion is a ROUTING signal, not an execution failure:
            # the workflow breaks to CONCLUDING with the gathered corpus
            # instead of dying mid-flight (previously this raised out of the
            # activity, failed the whole execution, and stranded the aggregate
            # in a non-terminal state with no audit record).
            logger.warning(
                "execute_action_activity budget exhausted; routing to conclude",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="execute_action_activity budget exhausted",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
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
                            "status": "BUDGET_EXCEEDED",
                            "budget_exceeded": True,
                            "items_count": 0,
                        },
                    },
                    activity="execute_action_activity",
                    investigation_id=inv_id_str,
                )
            return GenericActivityResult(
                success=True,
                data={
                    "status": "BUDGET_EXCEEDED",
                    "budget_exceeded": True,
                    "items_count": 0,
                },
            )
        except Exception as exc:
            logger.exception(
                "budget ledger check failed; failing closed",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="budget ledger check failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
            )
            raise _application_failure_from_exc(exc) from exc

        try:
            await action_service.authorize(
                tenant_id,
                investigation_id,
                inv_action,
                principal_id,
                application_id=application_id or None,
            )
        except Exception as exc:
            logger.exception(
                "execute_action_activity denied by authorization gate",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="execute_action_activity denied by authorization gate",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    action_type=action_type,
                ),
            )
            raise _application_failure_from_exc(exc) from exc

        # Dispatch via EvidenceGateway using an exhaustive enum-to-handler
        # registry (F-023) — unknown/unsupported actions are a hard failure,
        # never a silent no-op "EXECUTED".
        gateway: Any = getattr(ctx, "evidence_gateway", None)
        evidence_repo: Any = getattr(ctx, "evidence_repo", None)
        profile_repo: Any = getattr(ctx, "profile_repo", None)
        environment = "production"
        if profile_repo is not None and application_id:
            profile = await profile_repo.get_by_application_id(tenant_id, application_id)
            if profile is not None:
                environment = profile.environment

        result_status = "EXECUTED"
        items_count = 0

        async def _handle_query_state() -> int:
            from investigation_agent_platform.domain.evidence.requests import (
                ApplicationStateRequest,
            )

            req = ApplicationStateRequest(
                environment=environment,
                template_id=str(parameters.get("template_id", "default")),
                parameters=parameters,
            )
            qres = await gateway.get_application_state(
                tenant_id, investigation_id, application_id or "example-app", req
            )
            if evidence_repo is not None and qres.items:
                await evidence_repo.save_batch(tenant_id, qres.items, investigation_id)
            return len(qres.items)

        async def _handle_search_logs() -> int:
            from investigation_agent_platform.domain.evidence.requests import (
                RuntimeEvidenceRequest,
            )

            # Part 9: clamp caller-controlled dimensions to the domain bounds
            # (mirrors the provider-side clamp that applied before: min()
            # semantics preserved; per-item lengths still reject as invalid).
            req = RuntimeEvidenceRequest(
                environment=environment,
                keywords=list(parameters.get("keywords", []))[:20] or [],
                services=list(parameters.get("services", []))[:10] or [],
                limit=min(int(parameters.get("limit", 100)), 200),
            )
            qres = await gateway.search_runtime_evidence(
                tenant_id, investigation_id, application_id or "example-app", req
            )
            if evidence_repo is not None and qres.items:
                await evidence_repo.save_batch(tenant_id, qres.items, investigation_id)
            return len(qres.items)

        async def _handle_get_code() -> int:
            from investigation_agent_platform.domain.evidence.requests import SourceRequest

            req = SourceRequest(
                repository=application_id or "example-app",
                file_path=str(parameters.get("file_path", "")),
            )
            evidence = await gateway.get_source(
                tenant_id, investigation_id, application_id or "example-app", req
            )
            if evidence_repo is not None:
                await evidence_repo.save(tenant_id, evidence, investigation_id)
            return 1

        async def _handle_correlate() -> int:
            root_ids = [UUID(str(i)) for i in parameters.get("root_evidence_ids", [])]
            # Agent depth is clamped to the profile's correlation depth: the
            # planner may ask for less, never more.
            max_depth = min(int(parameters.get("max_depth", 2)), app_correlation_depth)
            qres = await gateway.correlate(
                tenant_id, investigation_id, application_id or "example-app", root_ids, max_depth
            )
            if evidence_repo is not None and qres.items:
                await evidence_repo.save_batch(tenant_id, qres.items, investigation_id)
            return len(qres.items)

        async def _handle_internal_reasoning_action() -> int:
            # VERIFY_HYPOTHESIS / CONCLUDE never touch an external evidence
            # provider — they operate purely on already persisted domain
            # state, so there is nothing to dispatch here.
            #
            # Hypothesis persistence happens for EVERY action type, not just
            # FORMULATE_HYPOTHESIS: the model attaches hypotheses_evaluated
            # to whatever action it proposes (often VERIFY_HYPOTHESIS), so
            # gating persistence on FORMULATE leaves the repo empty forever —
            # verification can never verify, and the ConclusionGate can never
            # approve COMPLETED. Statements dedupe against existing rows.
            if not proposed_hypotheses:
                return 0
            hypothesis_repo: Any = getattr(ctx, "hypothesis_repo", None)
            if hypothesis_repo is None:
                return 0
            from investigation_agent_platform.domain.hypothesis.models import (
                Hypothesis,
            )

            try:
                existing = await hypothesis_repo.find_by_investigation_id(
                    tenant_id, investigation_id
                )
            except Exception as exc:
                logger.exception(
                    "hypothesis dedupe read failed",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="hypothesis dedupe read failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
                )
                existing = []
            known = {str(getattr(h, "statement", "")).strip() for h in (existing or [])}
            saved = 0
            for text in proposed_hypotheses[:20]:
                statement = str(text or "").strip()[:2048]
                if not statement or statement in known:
                    continue
                known.add(statement)
                try:
                    await hypothesis_repo.save(
                        tenant_id,
                        Hypothesis(
                            tenant_id=tenant_id,
                            investigation_id=investigation_id,
                            title=statement[:256],
                            description=statement[:4096],
                            statement=statement,
                        ),
                        investigation_id,
                    )
                    saved += 1
                except Exception as exc:
                    logger.exception(
                        "hypothesis persistence failed",
                        extra=_log_extra(
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event="hypothesis persistence failed",
                            error=str(exc),
                            error_type=type(exc).__name__,
                        ),
                    )
            return saved

        _HANDLERS: dict[Any, Any] = {
            ActionType.QUERY_STATE: _handle_query_state,
            ActionType.SEARCH_LOGS: _handle_search_logs,
            ActionType.GET_CODE: _handle_get_code,
            ActionType.CORRELATE: _handle_correlate,
            ActionType.FORMULATE_HYPOTHESIS: _handle_internal_reasoning_action,
            ActionType.VERIFY_HYPOTHESIS: _handle_internal_reasoning_action,
            ActionType.CONCLUDE: _handle_internal_reasoning_action,
        }

        handler = _HANDLERS.get(resolved_action_type)
        if handler is None:
            # Exhaustive registry: this should be unreachable given the
            # ActionType(action_type) resolution above, but fail closed if a
            # new enum member is ever added without a handler.
            raise ApplicationFailure(
                f"No execution handler registered for action type: {resolved_action_type.value}",
                type="UnhandledActionTypeError",
                non_retryable=True,
            )

        if resolved_action_type in _ACTION_REQUIRED_EVIDENCE_TYPE_LOCAL and gateway is None:
            raise ApplicationFailure(
                "Evidence gateway is not configured; cannot execute evidence-producing action",
                type="EVIDENCE_PROVIDER_UNAVAILABLE",
                non_retryable=True,
            )

        try:
            items_count = await handler()
            # Operational telemetry: action type + result counts only (values
            # ride as extra keys per TO_DO-007/019; lazy %-style message).
            logger.warning(
                "execute_action_activity dispatched action=%s items=%s hypotheses=%s",
                resolved_action_type.value,
                items_count,
                len(proposed_hypotheses),
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="execute_action_activity dispatched",
                    action=resolved_action_type.value,
                    items_count=items_count,
                    hypotheses_count=len(proposed_hypotheses),
                ),
            )
        except ApplicationFailure:
            raise
        except Exception as exc:
            logger.exception(
                "execute_action_activity gateway dispatch failed",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="execute_action_activity gateway dispatch failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    action_type=action_type,
                ),
            )
            raise _application_failure_from_exc(exc) from exc

        # Audit action execution (always — the authorized action must always
        # be recorded, whether or not a gateway is wired).
        try:
            await action_service.execute(
                tenant_id, investigation_id, inv_action, principal_id=principal_id
            )
        except Exception as exc:
            logger.exception(
                "execute_action_activity record_action failed",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="execute_action_activity record_action failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
            )
            raise _application_failure_from_exc(exc) from exc

        if _idem_active_key is not None:
            await _complete_reservation(
                ctx=ctx,
                tenant_id=tenant_id,
                key=_idem_active_key,
                operation=_idem_operation,
                response={
                    "success": True,
                    "data": {"status": result_status, "items_count": items_count},
                },
                activity="execute_action_activity",
                investigation_id=inv_id_str,
            )
        logger.info(
            "execute_action_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="execute_action_activity completed",
                outcome="executed",
                action=resolved_action_type.value,
                items_count=items_count,
            ),
        )
        return GenericActivityResult(
            success=True, data={"status": result_status, "items_count": items_count}
        )
    except ApplicationFailure:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="execute_action_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        raise
    except Exception as exc:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="execute_action_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        logger.exception(
            "execute_action_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="execute_action_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def verify_root_cause_activity(
    params: VerifyRootCauseInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — pure compute over persisted state,
    # no durable writes of its own; re-execution is safe by construction.
    data = _coerced_data(VerifyRootCauseInput, params, activity="verify_root_cause_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "verify_root_cause_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="verify_root_cause_activity started",
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        if not tenant_id or not inv_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and investigation_id required"
            )
        investigation_id = UUID(inv_id_str)
        ctx = _get_ctx()
        evidence_repo: Any = getattr(ctx, "evidence_repo", None)
        hypothesis_repo: Any = getattr(ctx, "hypothesis_repo", None)

        evidence_items: list[Any] = []
        hypotheses: list[Any] = []
        if evidence_repo is not None:
            try:
                evidence_items = await evidence_repo.find_by_investigation_id(
                    tenant_id, investigation_id
                )
            except Exception as exc:
                logger.exception(
                    "verify_root_cause_activity evidence fetch failed",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="verify_root_cause_activity evidence fetch failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
                )
                raise _application_failure_from_exc(exc) from exc
        if hypothesis_repo is not None:
            try:
                hypotheses = await hypothesis_repo.find_by_investigation_id(
                    tenant_id, investigation_id
                )
            except Exception as exc:
                logger.exception(
                    "verify_root_cause_activity hypothesis fetch failed",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="verify_root_cause_activity hypothesis fetch failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
                )
                raise _application_failure_from_exc(exc) from exc

        if not hypotheses:
            return GenericActivityResult(
                success=True, data={"verified": False, "reason": "no_hypotheses"}
            )

        from investigation_agent_platform.application.investigation.verification import (
            RootCauseVerificationPolicy,
            VerificationEngine,
        )

        engine = VerificationEngine()
        # Evaluate first hypothesis; pass empty contradictions/causal_chain as minimal wiring.
        hypothesis = hypotheses[0]
        policy = RootCauseVerificationPolicy()
        result = engine.evaluate_hypothesis(hypothesis, evidence_items, [], [], policy)

        verified = result.status.value == "VERIFIED"
        logger.info(
            "verify_root_cause_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="verify_root_cause_activity completed",
                outcome="verified" if verified else "unverified",
                verification_status=result.status.value,
            ),
        )
        return GenericActivityResult(
            success=True,
            data={
                "verified": verified,
                "status": result.status.value,
                "confidence": result.confidence.overall_confidence,
                "hypothesis_id": str(result.hypothesis_id),
            },
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "verify_root_cause_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="verify_root_cause_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def checkpoint_activity(params: CheckpointInput) -> GenericActivityResult:
    data = _coerced_data(CheckpointInput, params, activity="checkpoint_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "checkpoint_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="checkpoint_activity started",
            step=data.get("step", 0),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        step = int(data.get("step", 0))
        state_snapshot: dict[str, Any] | None = data.get("state_snapshot")
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
                data=dict(_replayed.get("data") or {"step": step}),
                error=_replayed.get("error"),
            )

        checkpoint_repo: Any = getattr(ctx, "checkpoint_repo", None)
        # Also try _InMemoryCheckpointRepository attached via resume service pattern.
        if checkpoint_repo is None:
            # No repo wired — treat as no-op but succeed so workflow progresses.
            logger.error(
                "checkpoint_activity repo unwired; checkpoint not persisted",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="checkpoint_activity repo unwired",
                    step=step,
                ),
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
                        state_snapshot = st.model_dump(mode="json")
                    else:
                        state_snapshot = {"step": step}
                except Exception as exc:
                    logger.exception(
                        "checkpoint_activity snapshot build failed",
                        extra=_log_extra(
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event="checkpoint_activity snapshot build failed",
                            error=str(exc),
                            error_type=type(exc).__name__,
                            step=step,
                        ),
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
        logger.info(
            "checkpoint_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="checkpoint_activity completed",
                outcome="checkpointed",
                step=step,
            ),
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
        logger.exception(
            "checkpoint_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="checkpoint_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
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
            logger.error(
                "transition audit repo unwired; edge %s -> %s not recorded",
                getattr(from_status, "value", str(from_status)),
                getattr(to_status, "value", str(to_status)),
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="transition audit repo unwired",
                    from_status=getattr(from_status, "value", str(from_status)),
                    to_status=getattr(to_status, "value", str(to_status)),
                ),
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
                logger.error(
                    "transition audit actor downgraded; repo signature predates actor",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="transition audit actor downgraded",
                        actor=actor_value,
                    ),
                )
        except Exception as exc:
            # Audit recording must never break the lifecycle itself.
            audit_degraded = True
            logger.exception(
                "transition audit record failed",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="transition audit record failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
            )

    async def _append_timeline(from_status: Any, to_status: Any) -> None:
        nonlocal audit_degraded
        if timeline_repo is None:
            audit_degraded = True
            logger.error(
                "timeline repo unwired; STATE_CHANGE event not appended",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="timeline repo unwired",
                    from_status=getattr(from_status, "value", str(from_status)),
                    to_status=getattr(to_status, "value", str(to_status)),
                ),
            )
            return
        try:
            from investigation_agent_platform.domain.timeline.models import (
                TimelineEvent,
                TimelineEventType,
            )

            existing = await timeline_repo.find_by_investigation_id(
                tenant_id, inv.id
            )
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
            logger.exception(
                "timeline append failed",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="timeline append failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
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
async def transition_investigation_activity(
    params: TransitionInvestigationInput,
) -> GenericActivityResult:
    data = _coerced_data(
        TransitionInvestigationInput, params, activity="transition_investigation_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "transition_investigation_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="transition_investigation_activity started",
            target_status=str(data.get("target_status", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        target_status = str(data.get("target_status", ""))
        reason = str(data.get("reason", "")) or f"Transition to {target_status}"
        actor = str(data.get("actor", "SYSTEM")) or "SYSTEM"
        if not tenant_id or not inv_id_str or not target_status:
            return GenericActivityResult(
                success=False, error="tenant_id, investigation_id and target_status required"
            )
        investigation_id = UUID(inv_id_str)
        ctx = _get_ctx()
        inv_repo: Any = getattr(ctx, "investigation_repo", None)
        if inv_repo is None:
            logger.error(
                "transition_investigation_activity repo unwired; transition not applied",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="transition_investigation_activity repo unwired",
                    target_status=target_status,
                ),
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

        _, audit_degraded = await _transition_via_path(
            tenant_id,
            inv,
            target,
            reason,
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
        logger.info(
            "transition_investigation_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="transition_investigation_activity completed",
                outcome="transitioned",
                target_status=target.value,
                actor=actor,
                audit_degraded=audit_degraded,
            ),
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
        logger.exception(
            "transition_investigation_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="transition_investigation_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


async def _gate_conclusion_or_fail(tenant_id: str, investigation_id: UUID, ctx: Any) -> Any:
    """Evaluate ConclusionGate for a requested COMPLETED conclusion (F-051).

    Returns ``InvestigationStatus.COMPLETED`` only when the gate approves;
    otherwise returns ``InvestigationStatus.FAILED`` so the denial and its
    blockers are persisted in the lifecycle rather than swallowed. On
    approval, also persists the Finding + InvestigationConclusion that
    justify COMPLETED (Part 11.2) — the conclusion read path and clustering
    (Part 11.7+) depend on this corpus actually existing.
    """
    from investigation_agent_platform.application.investigation.verification import (
        ConclusionGate,
        RootCauseVerificationPolicy,
        VerificationEngine,
        VerificationStatus,
    )
    from investigation_agent_platform.domain.finding.models import (
        ConclusionStatus,
        Finding,
        FindingType,
        InvestigationConclusion,
    )
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
        logger.warning(
            "ConclusionGate: no hypotheses; denying COMPLETED",
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                event="ConclusionGate denied COMPLETED",
                reason="no_hypotheses",
            ),
        )
        return InvestigationStatus.FAILED

    engine = VerificationEngine()
    policy = RootCauseVerificationPolicy()
    verification = engine.evaluate_hypothesis(hypotheses[0], evidence_items, [], [], policy)
    gate = ConclusionGate()
    # NOTE (F-024 follow-up): the workflow does not yet emit causal chains or
    # persist contradictions, so the gate is evaluated with empty lists and a
    # policy copy that does not require a causal chain. Everything else —
    # verification status, confidence threshold, unresolved contradictions,
    # evidence presence/provenance/freshness — is enforced hard. Flip
    # require_causal_relationship back to True once the agentic loop produces
    # causal chains end-to-end.
    gate_policy = policy.model_copy(update={"require_causal_relationship": False})
    decision = await gate.evaluate(
        tenant_id, investigation_id, verification, [], [], evidence_items, gate_policy
    )
    if not decision.approved:
        logger.warning(
            "ConclusionGate denied COMPLETED: %s",
            "; ".join(decision.blockers),
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                event="ConclusionGate denied COMPLETED",
                blockers=list(decision.blockers),
            ),
        )
        return InvestigationStatus.FAILED

    # Part 11.2: persist the Finding + InvestigationConclusion that back this
    # approval. Best-effort — a persistence failure here must never revert an
    # already-approved conclusion to FAILED (the gate decision stands), but it
    # is logged loudly since downstream reads/clustering depend on this row.
    if finding_repo is not None:
        try:
            evidence_ids = [e.evidence_id for e in evidence_items][:100]
            hyp = hypotheses[0]
            finding = Finding(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                finding_type=FindingType.ROOT_CAUSE,
                title=hyp.title,
                statement=hyp.statement,
                evidence_ids=evidence_ids,
                related_hypothesis_ids=[hyp.id],
                causal_chain=evidence_ids[:1],
                confidence=verification.confidence.overall_confidence,
            )
            await finding_repo.save_finding_record(tenant_id, finding)
            conclusion = InvestigationConclusion(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                status=ConclusionStatus.ROOT_CAUSE_ESTABLISHED
                if verification.status == VerificationStatus.VERIFIED
                else ConclusionStatus.ROOT_CAUSE_LIKELY,
                root_cause=hyp.statement,
                supporting_evidence_ids=evidence_ids,
                supporting_hypothesis_ids=[hyp.id],
                confidence=verification.confidence.overall_confidence,
            )
            await finding_repo.save_conclusion(tenant_id, conclusion)
        except Exception as exc:
            logger.exception(
                "Failed to persist finding/conclusion after gate approval",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    event="finding/conclusion persist failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
            )
    return InvestigationStatus.COMPLETED


@activity.defn
async def conclude_investigation_activity(
    params: ConcludeInvestigationInput,
) -> GenericActivityResult:
    data = _coerced_data(
        ConcludeInvestigationInput, params, activity="conclude_investigation_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "conclude_investigation_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="conclude_investigation_activity started",
            status=str(data.get("status", "COMPLETED")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        status = str(data.get("status", "COMPLETED"))
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
                        target = await _gate_conclusion_or_fail(tenant_id, investigation_id, ctx)
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
                            f"Workflow concluded with {target.value}",
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
                        logger.info(
                            "conclude_investigation_activity completed",
                            extra=_log_extra(
                                tenant_id=tenant_id,
                                investigation_id=inv_id_str,
                                event="conclude_investigation_activity completed",
                                outcome="concluded",
                                status=target.value,
                                audit_degraded=audit_degraded,
                            ),
                        )
                    return GenericActivityResult(success=True, data={"status": target.value})
            except ApplicationFailure:
                raise
            except Exception as exc:
                logger.exception(
                    "conclude_investigation_activity transition failed",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="conclude_investigation_activity transition failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
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
        logger.exception(
            "conclude_investigation_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="conclude_investigation_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def publish_event_activity(
    params: PublishEventInput,
) -> GenericActivityResult:
    # TO_DO-011: not additionally idempotency-wired — the outbox row itself
    # IS the idempotency record (F-058: durable, idempotent on
    # (tenant_id, idempotency_key) with investigation_id:event_type key);
    # a second reserve/complete layer would be redundant.
    data = _coerced_data(PublishEventInput, params, activity="publish_event_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "publish_event_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="publish_event_activity started",
            event_type=str(data.get("event", "INVESTIGATION_FINISHED")),
        ),
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
                logger.exception(
                    "publish_event_activity investigation fetch failed",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="publish_event_activity investigation fetch failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
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
                logger.exception(
                    "publish_event_activity outbox enqueue failed",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="publish_event_activity outbox enqueue failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                        idempotency_key=idempotency_key,
                    ),
                )
                raise _application_failure_from_exc(exc) from exc
            if publisher is not None:
                try:
                    await outbox.dispatch_pending(tenant_id, publisher, batch_size=25)
                except Exception as exc:
                    logger.warning(
                        "publish_event_activity dispatch deferred; event stays queued",
                        extra=_log_extra(
                            tenant_id=tenant_id,
                            investigation_id=inv_id_str,
                            event="publish_event_activity dispatch deferred",
                            error=str(exc),
                            error_type=type(exc).__name__,
                        ),
                    )
            logger.info(
                "publish_event_activity completed",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="publish_event_activity completed",
                    outcome="queued",
                    event_type=event_type,
                ),
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
                logger.exception(
                    "publish_event_activity fallback failed",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="publish_event_activity fallback failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
                )
                raise _application_failure_from_exc(exc) from exc

        logger.info(
            "publish_event_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="publish_event_activity completed",
                outcome="published",
                event_type=event_type,
            ),
        )
        return GenericActivityResult(success=True, data={"event": event_type})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "publish_event_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="publish_event_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def retrieve_knowledge_activity(
    params: RetrieveKnowledgeInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — read-only retrieval; an empty
    # context degrades to unassisted reasoning rather than failing.
    """Validity-gated knowledge retrieval before reasoning (Part 6 D4, Slice 0).

    Returns verified facts + excluded_stale_count. Never raises on store
    trouble: an empty context degrades the investigation to unassisted
    reasoning rather than failing it.
    """
    data = _coerced_data(
        RetrieveKnowledgeInput, params, activity="retrieve_knowledge_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "retrieve_knowledge_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="retrieve_knowledge_activity started",
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        kinds = data.get("kinds")
        if not tenant_id or not inv_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and investigation_id required"
            )
        investigation_id = UUID(inv_id_str)
        ctx = _get_ctx()
        try:
            service = ctx.knowledge_retrieval_service()
            fingerprints = await _caller_fingerprints_for(ctx, tenant_id, investigation_id)
            context = await service.retrieve_for_reasoning(
                tenant_id,
                await _application_id_for(ctx, tenant_id, investigation_id),
                investigation_id,
                kinds=list(kinds) if kinds else None,
                code_issue_fingerprints=fingerprints,
            )
            return GenericActivityResult(
                success=True,
                data={
                    "verified_facts": [v.model_dump(mode="json") for v in context.verified_facts],
                    "preferences": [v.model_dump(mode="json") for v in context.preferences],
                    "temporal_summary": [
                        v.model_dump(mode="json") for v in context.temporal_summary
                    ],
                    "excluded_stale_count": context.excluded_stale_count,
                },
            )
        except Exception as exc:
            logger.exception(
                "retrieve_knowledge_activity degraded to empty context",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="retrieve_knowledge_activity degraded",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
            )
            return GenericActivityResult(
                success=True, data={"verified_facts": [], "excluded_stale_count": -1}
            )
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "retrieve_knowledge_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="retrieve_knowledge_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


async def _caller_fingerprints_for(ctx: Any, tenant_id: str, investigation_id: UUID) -> list[str]:
    """Membership proof for cross-tenant SHARED reads: fingerprints of code
    issues this tenant holds a session on, resolved from its own
    tenant-scoped rows only."""
    fingerprints: list[str] = []
    inv_repo: Any = getattr(ctx, "investigation_repo", None)
    if inv_repo is not None:
        investigation = await inv_repo.get_by_id(tenant_id, investigation_id)
        fingerprint = getattr(investigation, "code_issue_fingerprint", None)
        if fingerprint:
            fingerprints.append(fingerprint)
    return fingerprints


async def _application_id_for(ctx: Any, tenant_id: str, investigation_id: UUID) -> str:
    inv_repo: Any = getattr(ctx, "investigation_repo", None)
    if inv_repo is not None:
        investigation = await inv_repo.get_by_id(tenant_id, investigation_id)
        application_id = getattr(investigation, "application_id", None)
        if application_id:
            return str(application_id)
    return "unknown"


@activity.defn
async def capture_knowledge_activity(
    params: CaptureKnowledgeInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — best-effort post-conclusion capture
    # with a never-fail contract; a reserve/raise reservation would convert
    # degraded-0 outcomes into activity failures, violating that contract.
    """Distill concluded state into envelopes (Part 6 D4, Slice 0).

    Runs after conclusion. Capture failures never fail the investigation.
    """
    data = _coerced_data(
        CaptureKnowledgeInput, params, activity="capture_knowledge_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "capture_knowledge_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="capture_knowledge_activity started",
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        if not tenant_id or not inv_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and investigation_id required"
            )
        investigation_id = UUID(inv_id_str)
        ctx = _get_ctx()
        try:
            service = ctx.knowledge_capture_service()
            stored = await service.capture_for_investigation(tenant_id, investigation_id)
            logger.info(
                "capture_knowledge_activity completed",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="capture_knowledge_activity completed",
                    outcome="captured",
                    artifacts_stored=len(stored),
                ),
            )
            return GenericActivityResult(success=True, data={"artifacts_stored": len(stored)})
        except Exception as exc:
            logger.exception(
                "capture_knowledge_activity failed; investigation unaffected",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="capture_knowledge_activity degraded",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
            )
            return GenericActivityResult(success=True, data={"artifacts_stored": 0})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "capture_knowledge_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="capture_knowledge_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@dataclass
class SweepKnowledgeInput:
    """One janitor sweep pass over a set of tenants (ISSUE-9)."""

    tenants: list[str] = field(default_factory=list)
    reverify_conditional: bool = False


@activity.defn
async def sweep_knowledge_activity(
    params: SweepKnowledgeInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — the TTL sweep is idempotent by
    # design (re-running expires nothing new); never fails a workflow.
    """Proactive TTL-expiry sweep. Idempotent; never fails a workflow on error."""
    data = _coerced_data(SweepKnowledgeInput, params, activity="sweep_knowledge_activity")
    logger.info(
        "sweep_knowledge_activity started",
        extra=_log_extra(
            event="sweep_knowledge_activity started",
            tenants=list(data.get("tenants", []) or []),
        ),
    )
    try:
        from investigation_agent_platform.application.knowledge.janitor import (
            KnowledgeArtifactJanitor,
        )

        tenants = list(data.get("tenants", []) or [])
        reverify = bool(data.get("reverify_conditional", False))
        ctx = _get_ctx()
        total_expired = 0
        per_tenant: dict[str, int] = {}
        for tenant_id in tenants:
            try:
                retrieval = ctx.knowledge_retrieval_service()
                janitor = KnowledgeArtifactJanitor(ctx.artifact_repo, retrieval)
                result = await janitor.sweep_tenant(tenant_id, reverify_conditional=reverify)
                per_tenant[tenant_id] = result.expired_count
                total_expired += result.expired_count
            except Exception as exc:
                logger.exception(
                    "Knowledge sweep failed for tenant; continuing",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        event="sweep_knowledge_activity tenant failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    ),
                )
                per_tenant[tenant_id] = -1
        logger.info(
            "sweep_knowledge_activity completed",
            extra=_log_extra(
                event="sweep_knowledge_activity completed",
                outcome="swept",
                expired_total=total_expired,
            ),
        )
        return GenericActivityResult(
            success=True,
            data={"expired_total": total_expired, "per_tenant": per_tenant},
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "sweep_knowledge_activity failed",
            extra=_log_extra(
                event="sweep_knowledge_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@dataclass
class CollectSnapshotsInput:
    """One repository's snapshot-retention pass (ISSUE-4)."""

    tenant_id: str
    repository_id: str


@dataclass
class CollectSnapshotsOutput:
    collected_revisions: list[str] = field(default_factory=list)
    pinned_count: int = 0
    deleted_node_count: int = 0
    deleted_edge_count: int = 0
    error: str | None = None


@activity.defn
async def collect_snapshots_activity(
    params: CollectSnapshotsInput,
) -> CollectSnapshotsOutput:
    # TO_DO-011: not idempotency-wired — collection is idempotent by design
    # (already-collected snapshots report, never duplicate; audit nodes are
    # never deleted).
    """Classify one repository's topology snapshots and collect the
    unpinned, out-of-window ones (ISSUE-4 janitor).

    Never deletes a ``TopologySnapshot`` audit node, only the AST subgraph
    beneath it, and never touches anything pinned by an open investigation's
    own recorded evidence. Collection failures are reported, not raised —
    a transient Neo4j hiccup on one repository must not fail the whole
    scheduled pass (the workflow retries this activity per repository).
    """
    data = _coerced_data(
        CollectSnapshotsInput, params, activity="collect_snapshots_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "collect_snapshots_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="collect_snapshots_activity started",
            repository_id=str(data.get("repository_id", "")),
        ),
    )
    tenant_id = str(data.get("tenant_id", ""))
    repository_id = str(data.get("repository_id", ""))
    if not tenant_id or not repository_id:
        return CollectSnapshotsOutput(error="tenant_id and repository_id required")

    try:
        from datetime import UTC, datetime

        from investigation_agent_platform.application.topology.pinned_revisions import (
            EvidenceBackedPinnedRevisionsProvider,
        )
        from investigation_agent_platform.application.topology.retention import (
            classify_snapshots,
        )

        ctx = _get_ctx()
        topology_adapter: Any = getattr(ctx, "topology_adapter", None)
        if topology_adapter is None:
            return CollectSnapshotsOutput(error="topology_adapter is not configured")
        topology_config: Any = getattr(ctx, "topology_config", None)
        if topology_config is None or not getattr(topology_config, "retention_enabled", False):
            return CollectSnapshotsOutput(error="retention is not enabled")

        pinned_provider = EvidenceBackedPinnedRevisionsProvider(
            investigation_repo=ctx.investigation_repo,
            evidence_repo=ctx.evidence_repo,
        )
        pinned_revisions = await pinned_provider.list_pinned_revisions(tenant_id, repository_id)
        snapshots = await topology_adapter.list_snapshots(tenant_id, repository_id)
        classification = classify_snapshots(
            snapshots,
            pinned_revisions,
            now=datetime.now(UTC),
            retention_window_days=topology_config.retention_window_days,
            retention_max_snapshots_per_repository=(
                topology_config.retention_max_snapshots_per_repository
            ),
        )

        collected: list[str] = []
        deleted_nodes = 0
        deleted_edges = 0
        for snapshot in classification.collectible:
            result = await topology_adapter.collect_snapshot(
                tenant_id, repository_id, snapshot.revision
            )
            if not result.already_collected:
                collected.append(snapshot.revision)
                deleted_nodes += result.deleted_node_count
                deleted_edges += result.deleted_edge_count

        logger.info(
            "collect_snapshots_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="collect_snapshots_activity completed",
                outcome="collected",
                repository_id=repository_id,
                collected_count=len(collected),
            ),
        )
        return CollectSnapshotsOutput(
            collected_revisions=collected,
            pinned_count=len(classification.pinned),
            deleted_node_count=deleted_nodes,
            deleted_edge_count=deleted_edges,
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "collect_snapshots_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="collect_snapshots_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                repository_id=repository_id,
            ),
        )
        return CollectSnapshotsOutput(error=str(exc))


# ---------------------------------------------------------------------------
# Part 11.5: structured input requirements
# ---------------------------------------------------------------------------


@dataclass
class RecordInputRequirementInput:
    tenant_id: str
    investigation_id: str
    reason: str = ""
    json_schema: dict[str, Any] = field(default_factory=dict)
    classification: str = "INTERNAL"
    resume_status: str = "INVESTIGATING"
    promote_to_evidence: bool = False
    expires_in_seconds: int | None = None


@dataclass
class SetInputRequirementStateInput:
    tenant_id: str
    requirement_id: str
    expected_version: int
    state: str = "FULFILLED"


@dataclass
class PromoteFulfillmentEvidenceInput:
    tenant_id: str
    investigation_id: str
    requirement_id: str
    data: dict[str, Any] = field(default_factory=dict)
    classification: str = "INTERNAL"


@activity.defn
async def record_input_requirement_activity(
    params: RecordInputRequirementInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — each call mints a fresh requirement
    # row (new UUID), so no deterministic key is derivable from the inputs; a
    # caller-supplied idempotency key would need repository-level dedup owned
    # by Wave 3. The workflow emits one record call per input marker.
    """Persist one InputRequirement row and return its identity + wait budget."""
    from datetime import timedelta

    from investigation_agent_platform.domain.investigation.input_requirements import (
        AWAITING_INPUT_SOURCES,
        InputRequirement,
    )
    from investigation_agent_platform.domain.investigation.models import InvestigationStatus

    data = _coerced_data(
        RecordInputRequirementInput, params, activity="record_input_requirement_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "record_input_requirement_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="record_input_requirement_activity started",
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        reason = str(data.get("reason", "") or "Additional input required")
        if not tenant_id or not inv_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and investigation_id required"
            )
        investigation_id = UUID(inv_id_str)
        try:
            resume_status = InvestigationStatus(str(data.get("resume_status", "INVESTIGATING")))
        except ValueError:
            resume_status = InvestigationStatus.INVESTIGATING
        if resume_status not in AWAITING_INPUT_SOURCES:
            return GenericActivityResult(
                success=False,
                error=f"resume_status {resume_status.value} cannot suspend for input",
            )
        expires_in = data.get("expires_in_seconds")
        expires_at = None
        wait_timeout_seconds: float | None = None
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            from datetime import UTC, datetime

            expires_at = datetime.now(UTC) + timedelta(seconds=float(expires_in))
            wait_timeout_seconds = float(expires_in)
        requirement = InputRequirement(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            reason=reason[:512],
            json_schema=dict(data.get("json_schema", {}) or {}),
            classification=str(data.get("classification", "INTERNAL")),
            resume_status=resume_status,
            promote_to_evidence=bool(data.get("promote_to_evidence", False)),
            expires_at=expires_at,
        )
        ctx = _get_ctx()
        await ctx.input_repo.create_requirement(tenant_id, requirement)
        result_data: dict[str, Any] = {
            "requirement_id": str(requirement.requirement_id),
            "requirement_version": requirement.requirement_version,
            "resume_status": resume_status.value,
            "promote_to_evidence": requirement.promote_to_evidence,
        }
        if wait_timeout_seconds is not None:
            result_data["wait_timeout_seconds"] = wait_timeout_seconds
        logger.info(
            "record_input_requirement_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="record_input_requirement_activity completed",
                outcome="recorded",
                requirement_id=str(requirement.requirement_id),
            ),
        )
        return GenericActivityResult(success=True, data=result_data)
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "record_input_requirement_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="record_input_requirement_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def set_input_requirement_state_activity(
    params: SetInputRequirementStateInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — the compare-and-set on
    # (requirement_id, expected_version) IS the idempotency mechanism: a
    # retry with the same version conflicts loudly instead of duplicating.
    """Compare-and-set a requirement's state (FULFILLED/EXPIRED/CANCELLED)."""
    from investigation_agent_platform.domain.investigation.input_requirements import (
        RequirementState,
    )

    data = _coerced_data(
        SetInputRequirementStateInput, params, activity="set_input_requirement_state_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "set_input_requirement_state_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="set_input_requirement_state_activity started",
            requirement_id=str(data.get("requirement_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        req_id_str = str(data.get("requirement_id", ""))
        if not tenant_id or not req_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and requirement_id required"
            )
        try:
            state = RequirementState(str(data.get("state", "FULFILLED")))
        except ValueError:
            return GenericActivityResult(
                success=False, error=f"unknown requirement state {data.get('state')!r}"
            )
        try:
            expected_version = int(data.get("expected_version", 1))
        except (TypeError, ValueError):
            return GenericActivityResult(success=False, error="expected_version invalid")
        ctx = _get_ctx()
        await ctx.input_repo.set_state(tenant_id, UUID(req_id_str), expected_version, state)
        logger.info(
            "set_input_requirement_state_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="set_input_requirement_state_activity completed",
                outcome="state_set",
                requirement_id=req_id_str,
                state=state.value,
            ),
        )
        return GenericActivityResult(success=True, data={"state": state.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "set_input_requirement_state_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="set_input_requirement_state_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                requirement_id=str(data.get("requirement_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def promote_fulfillment_evidence_activity(
    params: PromoteFulfillmentEvidenceInput,
) -> GenericActivityResult:
    """Persist caller-supplied fulfillment data as DOCUMENTATION evidence.

    Only runs when the requirement opted in (``promote_to_evidence``); the
    classification travels from the requirement, and provenance records the
    requirement id so the evidence is traceable back to its source.
    """
    import hashlib
    import json
    from datetime import UTC, datetime

    from investigation_agent_platform.domain.evidence.models import (
        ClassificationLevel,
        Evidence,
        EvidenceType,
    )
    from investigation_agent_platform.domain.provenance.models import (
        EvidenceFreshness,
        EvidenceProvenance,
        QueryFingerprint,
        SourceLocation,
    )

    data = _coerced_data(
        PromoteFulfillmentEvidenceInput,
        params,
        activity="promote_fulfillment_evidence_activity",
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    logger.info(
        "promote_fulfillment_evidence_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="promote_fulfillment_evidence_activity started",
            requirement_id=str(data.get("requirement_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        req_id_str = str(data.get("requirement_id", ""))
        payload = data.get("data", {}) or {}
        if not tenant_id or not inv_id_str or not req_id_str:
            return GenericActivityResult(
                success=False,
                error="tenant_id, investigation_id and requirement_id required",
            )
        if not isinstance(payload, dict):
            return GenericActivityResult(success=False, error="fulfillment data invalid")
        try:
            classification = ClassificationLevel(str(data.get("classification", "INTERNAL")))
        except ValueError:
            classification = ClassificationLevel.INTERNAL
        investigation_id = UUID(inv_id_str)
        now = datetime.now(UTC)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        snippet = canonical[:4000]
        evidence = Evidence(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            evidence_type=EvidenceType.DOCUMENTATION,
            provider="input-fulfillment",
            source=f"input-requirement:{req_id_str}",
            title="Caller-supplied investigation input",
            summary=f"Fulfillment for input requirement {req_id_str}",
            content_snippet=snippet,
            attributes={"requirement_id": req_id_str},
            observed_at=None,
            retrieved_at=now,
            provenance=EvidenceProvenance(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                provider_type="input-fulfillment",
                requested_provider_id="input-fulfillment",
                actual_provider_id="input-fulfillment",
                source_system="input-requirement",
                retrieval_timestamp=now,
                query_fingerprint=QueryFingerprint(
                    provider_type="input-fulfillment",
                    operation="fulfill",
                    normalized_query_hash=hashlib.sha256(req_id_str.encode()).hexdigest(),
                ),
                source_location=SourceLocation(system="input-requirement", identifier=req_id_str),
            ),
            freshness=EvidenceFreshness(observed_at=None, retrieved_at=now),
            classification=classification,
            fingerprint=hashlib.sha256(
                f"input-fulfillment:{req_id_str}:{canonical}".encode()
            ).hexdigest(),
        )
        ctx = _get_ctx()
        # TO_DO-011: idempotency-wired — the evidence fingerprint is
        # deterministic over (requirement_id, canonical payload), so the
        # requirement id is the deterministic reservation key.
        _idem_active_key: str | None = None
        _idem_operation = "activity:promote_fulfillment"
        _idem_key = f"promote_fulfillment:{req_id_str}"
        _replayed, _idem_active_key = await _reserve_or_replay(
            ctx=ctx,
            tenant_id=tenant_id,
            key=_idem_key,
            request_payload={
                "tenant_id": tenant_id,
                "investigation_id": inv_id_str,
                "requirement_id": req_id_str,
            },
            operation=_idem_operation,
            activity="promote_fulfillment_evidence_activity",
            investigation_id=inv_id_str,
        )
        if _replayed is not None:
            return GenericActivityResult(
                success=bool(_replayed.get("success", True)),
                data=dict(_replayed.get("data") or {}),
                error=_replayed.get("error"),
            )
        await ctx.evidence_repo.save(tenant_id, evidence, investigation_id)
        if _idem_active_key is not None:
            await _complete_reservation(
                ctx=ctx,
                tenant_id=tenant_id,
                key=_idem_active_key,
                operation=_idem_operation,
                response={
                    "success": True,
                    "data": {"evidence_id": str(evidence.evidence_id)},
                },
                activity="promote_fulfillment_evidence_activity",
                investigation_id=inv_id_str,
            )
        logger.info(
            "promote_fulfillment_evidence_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="promote_fulfillment_evidence_activity completed",
                outcome="promoted",
                requirement_id=req_id_str,
                evidence_id=str(evidence.evidence_id),
            ),
        )
        return GenericActivityResult(success=True, data={"evidence_id": str(evidence.evidence_id)})
    except ApplicationFailure:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="promote_fulfillment_evidence_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        raise
    except Exception as exc:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="promote_fulfillment_evidence_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        logger.exception(
            "promote_fulfillment_evidence_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                investigation_id=inv_id_probe,
                event="promote_fulfillment_evidence_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


# ---------------------------------------------------------------------------
# Part 11.7: finding clustering
# ---------------------------------------------------------------------------


@dataclass
class RunClusteringInput:
    tenant_id: str
    job_id: str = ""
    limit: int = 500
    batch_size: int = 25


@dataclass
class UpdateClusteringJobInput:
    tenant_id: str
    job_id: str
    status: str = ""
    stage: dict[str, Any] | None = None
    error: str | None = None
    progress: int | None = None
    total: int | None = None


@activity.defn
async def run_clustering_activity(
    params: RunClusteringInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — a bounded incremental pass that is
    # re-runnable by design (re-running assigns nothing new); quota-exhaustion
    # returns a failure result rather than raising.
    """Run one bounded incremental clustering pass (Part 11.7)."""
    from investigation_agent_platform.application.finding.clustering_service import (
        FindingClusterService,
        QuotaExceededError,
    )
    from investigation_agent_platform.domain.common.extension import CapabilityScope
    from investigation_agent_platform.domain.common.quotas import QuotaPolicy

    data = _coerced_data(RunClusteringInput, params, activity="run_clustering_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "run_clustering_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="run_clustering_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        if not tenant_id:
            return GenericActivityResult(success=False, error="tenant_id required")
        try:
            limit = max(1, min(int(data.get("limit", 500)), 2000))
            batch_size = max(1, min(int(data.get("batch_size", 25)), 100))
        except (TypeError, ValueError):
            return GenericActivityResult(success=False, error="limit/batch_size invalid")
        ctx = _get_ctx()

        gateway: Any = None
        try:
            from investigation_agent_platform.infrastructure.configuration.config import (
                load_application_config_from_env,
            )
            from investigation_agent_platform.infrastructure.reasoning.factory import (
                create_llm_gateway,
            )

            gateway = create_llm_gateway(load_application_config_from_env().llm)
        except Exception as exc:
            # Lexical-only degraded mode: taxonomy preserved, new findings
            # explicitly UNASSIGNED with provenance. Loud, never silent.
            logger.exception(
                "Clustering without LLM gateway; lexical-only mode",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    event="run_clustering_activity lexical-only",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
            )

        quota_policy: QuotaPolicy | None = None
        quota_guarded = False
        try:
            from investigation_agent_platform.infrastructure.configuration.config import (
                load_application_config_from_env as _load_cfg,
            )

            quota_policy = QuotaPolicy.model_validate(_load_cfg().quotas.model_dump(mode="json"))
            quota_guarded = True
        except Exception as exc:
            logger.exception(
                "Clustering without quota guard; config unavailable",
                extra=_log_extra(
                    tenant_id=tenant_id,
                    event="run_clustering_activity quota unguarded",
                    error=str(exc),
                    error_type=type(exc).__name__,
                ),
            )

        service = FindingClusterService(
            finding_repo=getattr(ctx, "finding_repo", None),
            cluster_repo=getattr(ctx, "finding_cluster_repo", None),
            llm_gateway=gateway,
            embedder=None,
            batch_size=batch_size,
        )
        if service._finding_repo is None or service._cluster_repo is None:
            return GenericActivityResult(
                success=False, error="finding or cluster repository unavailable"
            )
        scope = CapabilityScope(tenant_id=tenant_id)
        try:
            result = await service.run_incremental(
                tenant_id,
                limit=limit,
                quota_enforcer=getattr(ctx, "quota_enforcer", None) if quota_guarded else None,
                quota_policy=quota_policy,
                scope=scope if quota_guarded else None,
            )
        except QuotaExceededError as exc:
            return GenericActivityResult(success=False, error=str(exc))
        result_data = result.model_dump(mode="json")
        result_data["quota_guarded"] = quota_guarded
        result_data["lexical_only"] = gateway is None
        logger.info(
            "run_clustering_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="run_clustering_activity completed",
                outcome="clustered",
                quota_guarded=quota_guarded,
            ),
        )
        return GenericActivityResult(success=True, data=result_data)
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "run_clustering_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="run_clustering_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def update_clustering_job_activity(
    params: UpdateClusteringJobInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — optimistic-concurrency versioning
    # (expected_version) IS the mechanism: a retried stale update conflicts
    # loudly instead of duplicating.
    """Apply a status/stage/error update to the linked BackgroundJob row."""
    from uuid import UUID as _UUID

    from investigation_agent_platform.domain.common.background_job import (
        BackgroundJobStage,
        BackgroundJobStatus,
    )

    data = _coerced_data(
        UpdateClusteringJobInput, params, activity="update_clustering_job_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "update_clustering_job_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="update_clustering_job_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id_str = str(data.get("job_id", ""))
        if not tenant_id or not job_id_str:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        try:
            job_id = _UUID(job_id_str)
        except ValueError:
            return GenericActivityResult(success=False, error="job_id invalid")
        ctx = _get_ctx()
        repo = getattr(ctx, "background_job_repo", None)
        if repo is None:
            return GenericActivityResult(success=False, error="background job repo unavailable")
        job = await repo.get_by_id(tenant_id, job_id)
        if job is None:
            return GenericActivityResult(success=False, error="background job not found")
        updates: dict[str, Any] = {"version": job.version + 1}
        status_raw = str(data.get("status", "") or "")
        if status_raw:
            try:
                updates["status"] = BackgroundJobStatus(status_raw)
            except ValueError:
                return GenericActivityResult(
                    success=False, error=f"unknown job status {status_raw!r}"
                )
        stage_raw = data.get("stage")
        if isinstance(stage_raw, dict) and stage_raw.get("name"):
            stages = list(job.stages)
            stages.append(
                BackgroundJobStage(
                    seq=len(stages),
                    name=str(stage_raw["name"])[:128],
                    message=str(stage_raw.get("message", ""))[:1024],
                )
            )
            updates["stages"] = stages[-50:]
        if data.get("error") is not None:
            updates["error"] = str(data["error"])[:2048]
        if isinstance(data.get("progress"), int):
            updates["progress"] = max(0, data["progress"])
        if isinstance(data.get("total"), int):
            updates["total"] = max(0, data["total"])
        updated = job.model_copy(update=updates)
        await repo.save(tenant_id, updated, expected_version=job.version)
        from investigation_agent_platform.infrastructure.messaging.job_fanout import (
            maybe_publish_job_progress,
        )

        await maybe_publish_job_progress(ctx, tenant_id, updated)
        logger.info(
            "update_clustering_job_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="update_clustering_job_activity completed",
                outcome="job_updated",
                job_id=job_id_str,
                status=updated.status.value,
            ),
        )
        return GenericActivityResult(success=True, data={"status": updated.status.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "update_clustering_job_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="update_clustering_job_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                job_id=str(data.get("job_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@dataclass
class RunReportInput:
    tenant_id: str
    job_id: str = ""
    legal_hold: bool = False


@dataclass
class UpdateReportJobInput:
    tenant_id: str
    job_id: str
    status: str = ""
    stage: dict[str, Any] | None = None
    error: str | None = None
    progress: int | None = None
    total: int | None = None


@activity.defn
async def run_report_activity(
    params: RunReportInput,
) -> GenericActivityResult:
    """Build, render, and store one aggregate report (Part 11.8)."""
    from uuid import UUID as _UUID

    from investigation_agent_platform.application.reporting.aggregate_service import (
        InvestigationAggregateReportService,
    )
    from investigation_agent_platform.application.reporting.artifacts import (
        store_report_run,
    )
    from investigation_agent_platform.infrastructure.reporting.html_renderer import (
        HtmlAggregateReportRenderer,
    )

    data = _coerced_data(RunReportInput, params, activity="run_report_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "run_report_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="run_report_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id_str = str(data.get("job_id", ""))
        if not tenant_id:
            return GenericActivityResult(success=False, error="tenant_id required")
        try:
            job_id = _UUID(job_id_str) if job_id_str else None
        except ValueError:
            return GenericActivityResult(success=False, error="job_id invalid")
        legal_hold = bool(data.get("legal_hold", False))
        ctx = _get_ctx()
        cluster_repo = getattr(ctx, "finding_cluster_repo", None)
        store = getattr(ctx, "artifact_store", None)
        if cluster_repo is None or store is None:
            return GenericActivityResult(
                success=False, error="cluster repository or artifact store unavailable"
            )
        service = InvestigationAggregateReportService(
            cluster_repo=cluster_repo,
            finding_repo=getattr(ctx, "finding_repo", None),
        )
        report = await service.build(tenant_id, job_id=job_id)
        html = HtmlAggregateReportRenderer().render(report)
        manifest_json = report.model_dump_json(indent=2).encode("utf-8")
        if job_id is None:
            return GenericActivityResult(
                success=False, error="job_id required for artifact history"
            )
        # TO_DO-011: idempotency-wired — (job_id) is deterministic per report
        # run, so a retried run replays stored artifact keys instead of
        # writing duplicate history artifacts.
        _idem_active_key: str | None = None
        _idem_operation = "activity:run_report"
        _idem_key = f"run_report:{job_id}"
        _replayed, _idem_active_key = await _reserve_or_replay(
            ctx=ctx,
            tenant_id=tenant_id,
            key=_idem_key,
            request_payload={
                "tenant_id": tenant_id,
                "job_id": job_id_str,
                "legal_hold": legal_hold,
            },
            operation=_idem_operation,
            activity="run_report_activity",
        )
        if _replayed is not None:
            return GenericActivityResult(
                success=bool(_replayed.get("success", True)),
                data=dict(_replayed.get("data") or {}),
                error=_replayed.get("error"),
            )
        refs = await store_report_run(
            store, tenant_id, job_id, html, manifest_json, legal_hold=legal_hold
        )
        await _link_report_result(ctx, tenant_id, job_id, refs["history_html"].key)
        if _idem_active_key is not None:
            await _complete_reservation(
                ctx=ctx,
                tenant_id=tenant_id,
                key=_idem_active_key,
                operation=_idem_operation,
                response={
                    "success": True,
                    "data": {
                        "taxonomy_revision": report.taxonomy_revision,
                        "assignment_count": report.assignment_count,
                        "artifact_keys": [refs["history_html"].key, refs["history_json"].key],
                    },
                },
                activity="run_report_activity",
            )
        logger.info(
            "run_report_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="run_report_activity completed",
                outcome="reported",
                job_id=job_id_str,
                assignment_count=report.assignment_count,
            ),
        )
        return GenericActivityResult(
            success=True,
            data={
                "taxonomy_revision": report.taxonomy_revision,
                "assignment_count": report.assignment_count,
                "artifact_keys": [refs["history_html"].key, refs["history_json"].key],
            },
        )
    except ApplicationFailure:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="run_report_activity",
        )
        raise
    except Exception as exc:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="run_report_activity",
        )
        logger.exception(
            "run_report_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="run_report_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                job_id=str(data.get("job_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


async def _link_report_result(ctx: Any, tenant_id: str, job_id: Any, result_ref: str) -> None:
    """Best-effort result_ref linkage; OCC races must not fail the run."""
    try:
        repo = getattr(ctx, "background_job_repo", None)
        if repo is None:
            return
        job = await repo.get_by_id(tenant_id, job_id)
        if job is None:
            return
        updated = job.model_copy(update={"result_ref": result_ref, "version": job.version + 1})
        await repo.save(tenant_id, updated, expected_version=job.version)
    except Exception as exc:
        logger.exception(
            "Report result_ref linkage skipped",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="report result_ref linkage skipped",
                error=str(exc),
                error_type=type(exc).__name__,
            ),
        )


@activity.defn
async def update_report_job_activity(
    params: UpdateReportJobInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — optimistic-concurrency versioning
    # (expected_version) IS the mechanism: a retried stale update conflicts
    # loudly instead of duplicating.
    """Apply a status/stage/error update to the linked aggregate-report job."""
    from uuid import UUID as _UUID

    from investigation_agent_platform.domain.common.background_job import (
        BackgroundJobStage,
        BackgroundJobStatus,
    )

    data = _coerced_data(UpdateReportJobInput, params, activity="update_report_job_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "update_report_job_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="update_report_job_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id_str = str(data.get("job_id", ""))
        if not tenant_id or not job_id_str:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        try:
            job_id = _UUID(job_id_str)
        except ValueError:
            return GenericActivityResult(success=False, error="job_id invalid")
        ctx = _get_ctx()
        repo = getattr(ctx, "background_job_repo", None)
        if repo is None:
            return GenericActivityResult(success=False, error="background job repo unavailable")
        job = await repo.get_by_id(tenant_id, job_id)
        if job is None:
            return GenericActivityResult(success=False, error="background job not found")
        updates: dict[str, Any] = {"version": job.version + 1}
        status_raw = str(data.get("status", "") or "")
        if status_raw:
            try:
                updates["status"] = BackgroundJobStatus(status_raw)
            except ValueError:
                return GenericActivityResult(
                    success=False, error=f"unknown job status {status_raw!r}"
                )
        stage_raw = data.get("stage")
        if isinstance(stage_raw, dict) and stage_raw.get("name"):
            stages = list(job.stages)
            stages.append(
                BackgroundJobStage(
                    seq=len(stages),
                    name=str(stage_raw["name"])[:128],
                    message=str(stage_raw.get("message", ""))[:1024],
                )
            )
            updates["stages"] = stages[-50:]
        if data.get("error") is not None:
            updates["error"] = str(data["error"])[:2048]
        if isinstance(data.get("progress"), int):
            updates["progress"] = max(0, data["progress"])
        if isinstance(data.get("total"), int):
            updates["total"] = max(0, data["total"])
        updated = job.model_copy(update=updates)
        await repo.save(tenant_id, updated, expected_version=job.version)
        from investigation_agent_platform.infrastructure.messaging.job_fanout import (
            maybe_publish_job_progress,
        )

        await maybe_publish_job_progress(ctx, tenant_id, updated)
        logger.info(
            "update_report_job_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="update_report_job_activity completed",
                outcome="job_updated",
                job_id=job_id_str,
                status=updated.status.value,
            ),
        )
        return GenericActivityResult(success=True, data={"status": updated.status.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "update_report_job_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="update_report_job_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                job_id=str(data.get("job_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@dataclass
class RunReindexInput:
    tenant_id: str
    job_id: str = ""
    source_id: str = ""
    application_id: str = ""


@dataclass
class UpdateReindexJobInput:
    tenant_id: str
    job_id: str
    status: str = ""
    stage: dict[str, Any] | None = None
    error: str | None = None
    progress: int | None = None
    total: int | None = None


@activity.defn
async def run_reindex_activity(
    params: RunReindexInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — index_source writes a new generation
    # per call and is re-runnable (a retry supersedes rather than duplicates);
    # generation-scoped dedup lives in the reference service.
    """Index one reference source into a new generation (Part 11.6)."""
    from investigation_agent_platform.application.reference.reference_service import (
        ReferenceDocumentService,
    )

    data = _coerced_data(RunReindexInput, params, activity="run_reindex_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "run_reindex_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="run_reindex_activity started",
            source_id=str(data.get("source_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        source_id = str(data.get("source_id", ""))
        if not tenant_id or not source_id:
            return GenericActivityResult(success=False, error="tenant_id and source_id required")
        ctx = _get_ctx()
        chunk_repo = getattr(ctx, "reference_repo", None)
        if chunk_repo is None:
            return GenericActivityResult(success=False, error="reference repository unavailable")
        service = ReferenceDocumentService(
            chunk_repo=chunk_repo,
            profile_repo=getattr(ctx, "profile_repo", None),
            embedder=None,
        )
        try:
            summary = await service.index_source(
                tenant_id, source_id, data.get("application_id") or None
            )
        except (RuntimeError, ValueError) as exc:
            return GenericActivityResult(success=False, error=str(exc))
        logger.info(
            "run_reindex_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="run_reindex_activity completed",
                outcome="indexed",
                source_id=source_id,
            ),
        )
        return GenericActivityResult(success=True, data=summary.model_dump(mode="json"))
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "run_reindex_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="run_reindex_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                source_id=str(data.get("source_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def update_reindex_job_activity(
    params: UpdateReindexJobInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — optimistic-concurrency versioning
    # (expected_version) IS the mechanism: a retried stale update conflicts
    # loudly instead of duplicating.
    """Apply a status/stage/error update to the linked reindex job."""
    from uuid import UUID as _UUID

    from investigation_agent_platform.domain.common.background_job import (
        BackgroundJobStage,
        BackgroundJobStatus,
    )

    data = _coerced_data(
        UpdateReindexJobInput, params, activity="update_reindex_job_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "update_reindex_job_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="update_reindex_job_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id_str = str(data.get("job_id", ""))
        if not tenant_id or not job_id_str:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        try:
            job_id = _UUID(job_id_str)
        except ValueError:
            return GenericActivityResult(success=False, error="job_id invalid")
        ctx = _get_ctx()
        repo = getattr(ctx, "background_job_repo", None)
        if repo is None:
            return GenericActivityResult(success=False, error="background job repo unavailable")
        job = await repo.get_by_id(tenant_id, job_id)
        if job is None:
            return GenericActivityResult(success=False, error="background job not found")
        updates: dict[str, Any] = {"version": job.version + 1}
        status_raw = str(data.get("status", "") or "")
        if status_raw:
            try:
                updates["status"] = BackgroundJobStatus(status_raw)
            except ValueError:
                return GenericActivityResult(
                    success=False, error=f"unknown job status {status_raw!r}"
                )
        stage_raw = data.get("stage")
        if isinstance(stage_raw, dict) and stage_raw.get("name"):
            stages = list(job.stages)
            stages.append(
                BackgroundJobStage(
                    seq=len(stages),
                    name=str(stage_raw["name"])[:128],
                    message=str(stage_raw.get("message", ""))[:1024],
                )
            )
            updates["stages"] = stages[-50:]
        if data.get("error") is not None:
            updates["error"] = str(data["error"])[:2048]
        if isinstance(data.get("progress"), int):
            updates["progress"] = max(0, data["progress"])
        if isinstance(data.get("total"), int):
            updates["total"] = max(0, data["total"])
        updated = job.model_copy(update=updates)
        await repo.save(tenant_id, updated, expected_version=job.version)
        from investigation_agent_platform.infrastructure.messaging.job_fanout import (
            maybe_publish_job_progress,
        )

        await maybe_publish_job_progress(ctx, tenant_id, updated)
        logger.info(
            "update_reindex_job_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="update_reindex_job_activity completed",
                outcome="job_updated",
                job_id=job_id_str,
                status=updated.status.value,
            ),
        )
        return GenericActivityResult(success=True, data={"status": updated.status.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "update_reindex_job_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="update_reindex_job_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                job_id=str(data.get("job_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@dataclass
class LoadBatchRecordsInput:
    tenant_id: str
    job_id: str = ""
    start_index: int = 0


@dataclass
class MarkBatchRecordsInput:
    tenant_id: str
    job_id: str = ""
    record_indices: list[int] = None  # type: ignore[assignment]
    status: str = ""
    child_workflow_ids: dict[str, str] | None = None


@dataclass
class MarkBatchRecordResultInput:
    tenant_id: str
    job_id: str = ""
    record_index: int = 0
    status: str = ""
    error: str | None = None


@dataclass
class ResolveBatchStragglersInput:
    tenant_id: str
    job_id: str = ""
    record_indices: list[int] = None  # type: ignore[assignment]


@dataclass
class CancelBatchChildrenInput:
    tenant_id: str
    job_id: str = ""


@dataclass
class UpdateBatchJobInput:
    tenant_id: str
    job_id: str
    status: str = ""
    stage: dict[str, Any] | None = None
    error: str | None = None
    progress: int | None = None
    total: int | None = None


def _batch_uuid(value: str) -> Any:
    from uuid import UUID as _UUID

    try:
        return _UUID(value)
    except ValueError:
        return None


@activity.defn
async def load_batch_records_activity(
    params: LoadBatchRecordsInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — read-only cursor load; no writes.
    """Load PENDING batch records from a dispatch cursor (Part 11.9)."""
    data = _coerced_data(
        LoadBatchRecordsInput, params, activity="load_batch_records_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "load_batch_records_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="load_batch_records_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id = _batch_uuid(str(data.get("job_id", "")))
        if not tenant_id or job_id is None:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        try:
            start_index = max(0, int(data.get("start_index", 0)))
        except (TypeError, ValueError):
            return GenericActivityResult(success=False, error="start_index invalid")
        ctx = _get_ctx()
        repo = getattr(ctx, "batch_repo", None)
        if repo is None:
            return GenericActivityResult(success=False, error="batch repository unavailable")
        records, total = await repo.list_records(tenant_id, job_id, limit=500, offset=0)
        pending = [
            {
                "record_index": record.record_index,
                "investigation_id": str(record.investigation_id)
                if record.investigation_id
                else None,
                "application_id": record.application_id,
                "external_key": record.external_key,
                "attempt": record.attempt,
            }
            for record in records
            if record.record_index >= start_index and record.status.value == "PENDING"
        ]
        logger.info(
            "load_batch_records_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="load_batch_records_activity completed",
                outcome="loaded",
                pending_count=len(pending),
                total=total,
            ),
        )
        return GenericActivityResult(success=True, data={"records": pending, "total": total})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "load_batch_records_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="load_batch_records_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                job_id=str(data.get("job_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def mark_batch_records_activity(
    params: MarkBatchRecordsInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — convergent status sets: re-marking
    # the same records with the same status/child ids is semantically a
    # no-op, so retries converge instead of duplicating.
    """Bulk-mark records DISPATCHED/RUNNING with child workflow ids."""
    from investigation_agent_platform.domain.intake.batch import BatchRecordStatus

    data = _coerced_data(
        MarkBatchRecordsInput, params, activity="mark_batch_records_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "mark_batch_records_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="mark_batch_records_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id = _batch_uuid(str(data.get("job_id", "")))
        if not tenant_id or job_id is None:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        try:
            status = BatchRecordStatus(str(data.get("status", "") or ""))
        except ValueError:
            return GenericActivityResult(success=False, error="unknown record status")
        indices = data.get("record_indices") or []
        child_ids = data.get("child_workflow_ids") or {}
        ctx = _get_ctx()
        repo = getattr(ctx, "batch_repo", None)
        if repo is None:
            return GenericActivityResult(success=False, error="batch repository unavailable")
        marked = 0
        for raw_index in indices:
            record = await repo.get_record(tenant_id, job_id, int(raw_index))
            if record is None:
                continue
            child_id = child_ids.get(str(raw_index))
            await repo.save_record(
                tenant_id,
                job_id,
                record.model_copy(update={"status": status, "child_workflow_id": child_id}),
            )
            marked += 1
        logger.info(
            "mark_batch_records_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="mark_batch_records_activity completed",
                outcome="marked",
                marked=marked,
            ),
        )
        return GenericActivityResult(success=True, data={"marked": marked})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "mark_batch_records_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="mark_batch_records_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                job_id=str(data.get("job_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def mark_batch_record_result_activity(
    params: MarkBatchRecordResultInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — terminal marking is convergent
    # (same status/error rewrite is a no-op); guarded to terminal states.
    """Mark one record terminal (DONE/FAILED/CANCELED) with an error."""
    from investigation_agent_platform.domain.intake.batch import BatchRecordStatus

    data = _coerced_data(
        MarkBatchRecordResultInput, params, activity="mark_batch_record_result_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "mark_batch_record_result_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="mark_batch_record_result_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id = _batch_uuid(str(data.get("job_id", "")))
        if not tenant_id or job_id is None:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        try:
            index = int(data.get("record_index", -1))
            status = BatchRecordStatus(str(data.get("status", "") or ""))
        except (TypeError, ValueError):
            return GenericActivityResult(success=False, error="record result invalid")
        if status not in (
            BatchRecordStatus.DONE,
            BatchRecordStatus.FAILED,
            BatchRecordStatus.CANCELED,
        ):
            return GenericActivityResult(success=False, error="status must be terminal")
        ctx = _get_ctx()
        repo = getattr(ctx, "batch_repo", None)
        if repo is None:
            return GenericActivityResult(success=False, error="batch repository unavailable")
        record = await repo.get_record(tenant_id, job_id, index)
        if record is None:
            return GenericActivityResult(success=False, error="batch record not found")
        error = data.get("error")
        await repo.save_record(
            tenant_id,
            job_id,
            record.model_copy(
                update={"status": status, "error": str(error)[:2048] if error else None}
            ),
        )
        logger.info(
            "mark_batch_record_result_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="mark_batch_record_result_activity completed",
                outcome="marked",
                status=status.value,
            ),
        )
        return GenericActivityResult(success=True, data={"status": status.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "mark_batch_record_result_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="mark_batch_record_result_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                job_id=str(data.get("job_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def resolve_batch_stragglers_activity(
    params: ResolveBatchStragglersInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — classification from persisted
    # investigation status is convergent (re-running re-derives the same
    # AWAITING_INPUT/RUNNING labels); never invents terminal states.
    """Classify still-running records via the persisted investigation status.

    AWAITING_INPUT investigations keep running (parent completes partial);
    anything else still active stays RUNNING (also left running). Never
    invents terminal states for live executions.
    """
    from investigation_agent_platform.domain.intake.batch import BatchRecordStatus

    data = _coerced_data(
        ResolveBatchStragglersInput, params, activity="resolve_batch_stragglers_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "resolve_batch_stragglers_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="resolve_batch_stragglers_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id = _batch_uuid(str(data.get("job_id", "")))
        if not tenant_id or job_id is None:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        indices = [int(index) for index in (data.get("record_indices") or [])]
        ctx = _get_ctx()
        repo = getattr(ctx, "batch_repo", None)
        investigation_repo = getattr(ctx, "investigation_repo", None)
        if repo is None or investigation_repo is None:
            return GenericActivityResult(success=False, error="repositories unavailable")
        awaiting = 0
        running = 0
        for index in indices:
            record = await repo.get_record(tenant_id, job_id, index)
            if record is None or record.investigation_id is None:
                continue
            investigation = await investigation_repo.get_by_id(tenant_id, record.investigation_id)
            status_value = str(getattr(getattr(investigation, "status", None), "value", ""))
            if status_value == "AWAITING_INPUT":
                await repo.save_record(
                    tenant_id,
                    job_id,
                    record.model_copy(update={"status": BatchRecordStatus.AWAITING_INPUT}),
                )
                awaiting += 1
            else:
                await repo.save_record(
                    tenant_id,
                    job_id,
                    record.model_copy(update={"status": BatchRecordStatus.RUNNING}),
                )
                running += 1
        logger.info(
            "resolve_batch_stragglers_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="resolve_batch_stragglers_activity completed",
                outcome="resolved",
                awaiting_input=awaiting,
                running=running,
            ),
        )
        return GenericActivityResult(
            success=True, data={"awaiting_input": awaiting, "running": running}
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "resolve_batch_stragglers_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="resolve_batch_stragglers_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                job_id=str(data.get("job_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def cancel_batch_children_activity(
    params: CancelBatchChildrenInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — Temporal workflow cancel is
    # idempotent per child handle and each outcome is recorded on the batch
    # row, so retries converge (already-canceled children are skipped by
    # their CANCELED status filter).
    """Cancel live child executions for RUNNING records (Part 11.9).

    Explicit propagation: the declared parent-close policy is ABANDON (so
    awaiting-input children survive parent completion), therefore parent
    cancellation fans out here — one client cancel per live child, each
    outcome recorded. Failures cancel auditably, never silently.
    """
    from investigation_agent_platform.domain.intake.batch import BatchRecordStatus

    data = _coerced_data(
        CancelBatchChildrenInput, params, activity="cancel_batch_children_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "cancel_batch_children_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="cancel_batch_children_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id = _batch_uuid(str(data.get("job_id", "")))
        if not tenant_id or job_id is None:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        ctx = _get_ctx()
        repo = getattr(ctx, "batch_repo", None)
        client = getattr(ctx, "temporal_client", None)
        if repo is None:
            return GenericActivityResult(success=False, error="batch repository unavailable")
        if client is None:
            return GenericActivityResult(success=False, error="temporal client unavailable")
        records, _ = await repo.list_records(tenant_id, job_id, limit=500, offset=0)
        canceled = 0
        failed: list[str] = []
        for record in records:
            if record.status not in (BatchRecordStatus.RUNNING, BatchRecordStatus.DISPATCHED):
                continue
            if not record.child_workflow_id:
                continue
            try:
                handle = client.get_workflow_handle(record.child_workflow_id)
                await handle.cancel()
                await repo.save_record(
                    tenant_id,
                    job_id,
                    record.model_copy(update={"status": BatchRecordStatus.CANCELED}),
                )
                canceled += 1
            except Exception as exc:
                logger.exception(
                    "cancel_batch_children_activity child cancel failed",
                    extra=_log_extra(
                        tenant_id=tenant_id,
                        event="cancel_batch_children_activity child cancel failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                        record_index=record.record_index,
                    ),
                )
                failed.append(f"{record.record_index}:{exc}")
        logger.info(
            "cancel_batch_children_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="cancel_batch_children_activity completed",
                outcome="canceled",
                canceled=canceled,
                failed_count=len(failed),
            ),
        )
        return GenericActivityResult(success=True, data={"canceled": canceled, "failed": failed})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "cancel_batch_children_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="cancel_batch_children_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                job_id=str(data.get("job_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def update_batch_job_activity(
    params: UpdateBatchJobInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — optimistic-concurrency versioning
    # (expected_version) IS the mechanism: a retried stale update conflicts
    # loudly instead of duplicating.
    """Apply a status/stage/error update to the linked batch job."""
    from uuid import UUID as _UUID

    from investigation_agent_platform.domain.common.background_job import (
        BackgroundJobStage,
        BackgroundJobStatus,
    )

    data = _coerced_data(UpdateBatchJobInput, params, activity="update_batch_job_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "update_batch_job_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="update_batch_job_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id_str = str(data.get("job_id", ""))
        if not tenant_id or not job_id_str:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        try:
            job_id = _UUID(job_id_str)
        except ValueError:
            return GenericActivityResult(success=False, error="job_id invalid")
        ctx = _get_ctx()
        repo = getattr(ctx, "background_job_repo", None)
        if repo is None:
            return GenericActivityResult(success=False, error="background job repo unavailable")
        job = await repo.get_by_id(tenant_id, job_id)
        if job is None:
            return GenericActivityResult(success=False, error="background job not found")
        updates: dict[str, Any] = {"version": job.version + 1}
        status_raw = str(data.get("status", "") or "")
        if status_raw:
            try:
                updates["status"] = BackgroundJobStatus(status_raw)
            except ValueError:
                return GenericActivityResult(
                    success=False, error=f"unknown job status {status_raw!r}"
                )
        stage_raw = data.get("stage")
        if isinstance(stage_raw, dict) and stage_raw.get("name"):
            stages = list(job.stages)
            stages.append(
                BackgroundJobStage(
                    seq=len(stages),
                    name=str(stage_raw["name"])[:128],
                    message=str(stage_raw.get("message", ""))[:1024],
                )
            )
            updates["stages"] = stages[-50:]
        if data.get("error") is not None:
            updates["error"] = str(data["error"])[:2048]
        if isinstance(data.get("progress"), int):
            updates["progress"] = max(0, data["progress"])
        if isinstance(data.get("total"), int):
            updates["total"] = max(0, data["total"])
        updated = job.model_copy(update=updates)
        await repo.save(tenant_id, updated, expected_version=job.version)
        from investigation_agent_platform.infrastructure.messaging.job_fanout import (
            maybe_publish_job_progress,
        )

        await maybe_publish_job_progress(ctx, tenant_id, updated)
        logger.info(
            "update_batch_job_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="update_batch_job_activity completed",
                outcome="job_updated",
                job_id=job_id_str,
                status=updated.status.value,
            ),
        )
        return GenericActivityResult(success=True, data={"status": updated.status.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "update_batch_job_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="update_batch_job_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                job_id=str(data.get("job_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc


@dataclass
class SummarizeBatchInput:
    tenant_id: str
    job_id: str = ""


@activity.defn
async def summarize_batch_activity(
    params: SummarizeBatchInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — pure aggregation over record rows;
    # no writes.
    """Aggregate batch counts over record rows (Part 11.9)."""
    data = _coerced_data(SummarizeBatchInput, params, activity="summarize_batch_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    logger.info(
        "summarize_batch_activity started",
        extra=_log_extra(
            tenant_id=tenant_id_probe,
            event="summarize_batch_activity started",
            job_id=str(data.get("job_id", "")),
        ),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id = _batch_uuid(str(data.get("job_id", "")))
        if not tenant_id or job_id is None:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        ctx = _get_ctx()
        repo = getattr(ctx, "batch_repo", None)
        if repo is None:
            return GenericActivityResult(success=False, error="batch repository unavailable")
        records, _ = await repo.list_records(tenant_id, job_id, limit=500, offset=0)
        counts = {
            "total": len(records),
            "succeeded": 0,
            "failed": 0,
            "awaiting_input": 0,
            "canceled": 0,
        }
        for record in records:
            status = record.status.value
            if status == "DONE":
                counts["succeeded"] += 1
            elif status == "FAILED":
                counts["failed"] += 1
            elif status == "AWAITING_INPUT":
                counts["awaiting_input"] += 1
            elif status == "CANCELED":
                counts["canceled"] += 1
        logger.info(
            "summarize_batch_activity completed",
            extra=_log_extra(
                tenant_id=tenant_id,
                event="summarize_batch_activity completed",
                outcome="summarized",
                total=counts["total"],
            ),
        )
        return GenericActivityResult(success=True, data=counts)
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception(
            "summarize_batch_activity failed",
            extra=_log_extra(
                tenant_id=tenant_id_probe,
                event="summarize_batch_activity failed",
                error=str(exc),
                error_type=type(exc).__name__,
                job_id=str(data.get("job_id", "")),
            ),
        )
        raise _application_failure_from_exc(exc) from exc
