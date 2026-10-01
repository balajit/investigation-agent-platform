# src/investigation_agent_platform/application/worker/activities.py
"""Temporal activity thin adapters — each delegates to the application/domain layer via AppContext."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
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
    step: int
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
    from investigation_agent_platform.api.dependencies import get_app_context

    return get_app_context()


@activity.defn
async def create_investigation_activity(
    params: CreateInvestigationInput,
) -> CreateInvestigationOutput:
    try:
        activity.logger.info(
            "create_investigation_activity",
            extra={"app_id": getattr(params, "application_id", None)},
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
        return CreateInvestigationOutput(investigation_id=str(inv.id), status=inv.status.value)
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("create_investigation_activity failed", extra={"error": str(exc)})
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def retrieve_evidence_activity(
    params: RetrieveEvidenceInput | dict[str, Any],
) -> GenericActivityResult:
    activity.logger.info("retrieve_evidence_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
                window_seconds = profile.investigation_configuration.default_time_window
                # Part 9: RuntimeEvidenceRequest caps limit at the provider
                # ceiling (200); clamp here so a generous profile knob cannot
                # fail request validation (provider clamped identically before).
                max_results = min(profile.investigation_configuration.max_evidence_per_query, 200)

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
                "retrieve_evidence_activity gateway call failed", extra={"error": str(exc)}
            )
            raise _application_failure_from_exc(exc) from exc
        return GenericActivityResult(
            success=True,
            data={"items_count": len(result.items), "total_count": result.total_count},
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("retrieve_evidence_activity failed", extra={"error": str(exc)})
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def reason_activity(params: ReasonInput | dict[str, Any]) -> GenericActivityResult:
    activity.logger.info("reason_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
                    extra={"error": str(exc)},
                )
                raise ApplicationFailure(
                    "Reasoning/safety policy could not be initialized; refusing to process",
                    type="SecurityPolicyInitializationError",
                    non_retryable=True,
                ) from exc

        # Build minimal InvestigationState for reasoning.
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
                        "reason_activity checkpoint hydration failed", extra={"error": str(exc)}
                    )
            if state is None:
                state = InvestigationState(investigation=investigation)

            decision = await coordinator.reason(tenant_id, state)
            return GenericActivityResult(
                success=True,
                data={
                    "conclusion_readiness": decision.conclusion_readiness,
                    "observations": decision.observations,
                    "iteration": iteration,
                },
            )
        except Exception as exc:
            logger.exception("reason_activity reasoning call failed", extra={"error": str(exc)})
            raise _application_failure_from_exc(exc) from exc
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("reason_activity failed", extra={"error": str(exc)})
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def execute_action_activity(
    params: ExecuteActionInput | dict[str, Any],
) -> GenericActivityResult:
    activity.logger.info("execute_action_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        action_type = str(data.get("action", "QUERY_STATE"))
        parameters: dict[str, Any] = dict(data.get("parameters", {}))
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
        except InvestigationLimitExceededException:
            raise
        except Exception as exc:
            logger.warning("budget ledger check failed; failing closed", extra={"error": str(exc)})
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
            logger.warning(
                "execute_action_activity denied by authorization gate",
                extra={"error": str(exc), "action_type": action_type, "tenant_id": tenant_id},
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
            max_depth = int(parameters.get("max_depth", 2))
            qres = await gateway.correlate(
                tenant_id, investigation_id, application_id or "example-app", root_ids, max_depth
            )
            if evidence_repo is not None and qres.items:
                await evidence_repo.save_batch(tenant_id, qres.items, investigation_id)
            return len(qres.items)

        async def _handle_internal_reasoning_action() -> int:
            # FORMULATE_HYPOTHESIS / VERIFY_HYPOTHESIS / CONCLUDE never touch an
            # external evidence provider — they operate purely on already
            # persisted domain state, so there is nothing to dispatch here.
            return 0

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
        except ApplicationFailure:
            raise
        except Exception as exc:
            logger.exception(
                "execute_action_activity gateway dispatch failed",
                extra={"error": str(exc), "action_type": action_type},
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
                "execute_action_activity record_action failed", extra={"error": str(exc)}
            )
            raise _application_failure_from_exc(exc) from exc

        return GenericActivityResult(
            success=True, data={"status": result_status, "items_count": items_count}
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("execute_action_activity failed", extra={"error": str(exc)})
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def verify_root_cause_activity(
    params: VerifyRootCauseInput | dict[str, Any],
) -> GenericActivityResult:
    activity.logger.info("verify_root_cause_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
                    "verify_root_cause_activity evidence fetch failed", extra={"error": str(exc)}
                )
                raise _application_failure_from_exc(exc) from exc
        if hypothesis_repo is not None:
            try:
                hypotheses = await hypothesis_repo.find_by_investigation_id(
                    tenant_id, investigation_id
                )
            except Exception as exc:
                logger.exception(
                    "verify_root_cause_activity hypothesis fetch failed", extra={"error": str(exc)}
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
        logger.exception("verify_root_cause_activity failed", extra={"error": str(exc)})
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def checkpoint_activity(params: CheckpointInput | dict[str, Any]) -> GenericActivityResult:
    activity.logger.info("checkpoint_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
        checkpoint_repo: Any = getattr(ctx, "checkpoint_repo", None)
        # Also try _InMemoryCheckpointRepository attached via resume service pattern.
        if checkpoint_repo is None:
            # No repo wired — treat as no-op but succeed so workflow progresses.
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
                    logger.warning(
                        "checkpoint_activity snapshot build failed", extra={"error": str(exc)}
                    )
                    state_snapshot = {"step": step}
            else:
                state_snapshot = {"step": step}

        await checkpoint_repo.save_checkpoint(
            tenant_id, investigation_id, step, state_snapshot or {"step": step}
        )
        return GenericActivityResult(success=True, data={"step": step})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("checkpoint_activity failed", extra={"error": str(exc)})
        raise _application_failure_from_exc(exc) from exc


def _find_transition_path(
    start: Any, target: Any, transitions: dict[Any, set[Any]]
) -> list[Any] | None:
    """BFS shortest path for graceful shutdown; returns list of statuses including start->target."""
    from collections import deque

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
    tenant_id: str, inv: Any, target: Any, reason: str, inv_repo: Any
) -> Any:
    """Walk domain graph step-by-step, persisting each transition."""
    # Reuse same valid_transitions as Investigation.transition_to for path finding
    # Build graph via temporary instance call by inspecting valid_transitions from method closure?
    # Instead import and reconstruct consistent graph via a dummy investigation.
    from investigation_agent_platform.domain.investigation.models import (
        ActorType,
        InvestigationStatus,
    )

    # Duplicate graph to avoid calling private logic - keep in sync with models.py
    graph: dict[InvestigationStatus, set[InvestigationStatus]] = {
        InvestigationStatus.CREATED: {
            InvestigationStatus.CONTEXTUALIZING,
            InvestigationStatus.INVESTIGATING,
            InvestigationStatus.CANCELLED,
            InvestigationStatus.FAILED,
        },
        InvestigationStatus.CONTEXTUALIZING: {
            InvestigationStatus.INVESTIGATING,
            InvestigationStatus.CORRELATING,
            InvestigationStatus.HYPOTHESIZING,
            InvestigationStatus.CONCLUDING,
            InvestigationStatus.CANCELLED,
            InvestigationStatus.FAILED,
        },
        InvestigationStatus.INVESTIGATING: {
            InvestigationStatus.CORRELATING,
            InvestigationStatus.HYPOTHESIZING,
            InvestigationStatus.CONCLUDING,
            InvestigationStatus.CANCELLED,
            InvestigationStatus.FAILED,
        },
        InvestigationStatus.CORRELATING: {
            InvestigationStatus.HYPOTHESIZING,
            InvestigationStatus.INVESTIGATING,
            InvestigationStatus.CONCLUDING,
            InvestigationStatus.CANCELLED,
            InvestigationStatus.FAILED,
        },
        InvestigationStatus.HYPOTHESIZING: {
            InvestigationStatus.VERIFYING,
            InvestigationStatus.INVESTIGATING,
            InvestigationStatus.CONCLUDING,
            InvestigationStatus.CANCELLED,
            InvestigationStatus.FAILED,
        },
        InvestigationStatus.VERIFYING: {
            InvestigationStatus.CONCLUDING,
            InvestigationStatus.HYPOTHESIZING,
            InvestigationStatus.INVESTIGATING,
            InvestigationStatus.CORRELATING,
            InvestigationStatus.CANCELLED,
            InvestigationStatus.FAILED,
        },
        InvestigationStatus.CONCLUDING: {
            InvestigationStatus.COMPLETED,
            InvestigationStatus.FAILED,
            InvestigationStatus.CANCELLED,
        },
        # Reopen edges mirror domain/investigation/models.py (Part 6 D8:
        # recurring code issues reopen closed investigations).
        InvestigationStatus.COMPLETED: {
            InvestigationStatus.INVESTIGATING,
        },
        InvestigationStatus.FAILED: {
            InvestigationStatus.INVESTIGATING,
        },
        InvestigationStatus.CANCELLED: set(),
    }
    path = _find_transition_path(inv.status, target, graph)
    if path is None:
        # No path - try direct transition to surface proper exception
        updated, _ = inv.transition_to(target, ActorType.SYSTEM, reason)
        await inv_repo.save(tenant_id, updated, expected_version=inv.version)
        return updated
    current = inv
    # Walk edges excluding start node
    for nxt in path[1:]:
        updated, _ = current.transition_to(nxt, ActorType.SYSTEM, reason)
        await inv_repo.save(tenant_id, updated, expected_version=current.version)
        current = updated
    return current


@activity.defn
async def transition_investigation_activity(
    params: TransitionInvestigationInput | dict[str, Any],
) -> GenericActivityResult:
    activity.logger.info("transition_investigation_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        target_status = str(data.get("target_status", ""))
        reason = str(data.get("reason", "")) or f"Transition to {target_status}"
        if not tenant_id or not inv_id_str or not target_status:
            return GenericActivityResult(
                success=False, error="tenant_id, investigation_id and target_status required"
            )
        investigation_id = UUID(inv_id_str)
        ctx = _get_ctx()
        inv_repo: Any = getattr(ctx, "investigation_repo", None)
        if inv_repo is None:
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
        await _transition_via_path(tenant_id, inv, target, reason, inv_repo)
        return GenericActivityResult(success=True, data={"status": target.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("transition_investigation_activity failed", extra={"error": str(exc)})
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
        logger.warning("ConclusionGate: no hypotheses; denying COMPLETED")
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
            extra={"tenant_id": tenant_id, "investigation_id": str(investigation_id)},
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
                extra={
                    "tenant_id": tenant_id,
                    "investigation_id": str(investigation_id),
                    "error": str(exc),
                },
            )
    return InvestigationStatus.COMPLETED


@activity.defn
async def conclude_investigation_activity(
    params: ConcludeInvestigationInput | dict[str, Any],
) -> GenericActivityResult:
    activity.logger.info("conclude_investigation_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
                        await _transition_via_path(
                            tenant_id,
                            inv,
                            target,
                            f"Workflow concluded with {target.value}",
                            inv_repo,
                        )
                    return GenericActivityResult(success=True, data={"status": target.value})
            except ApplicationFailure:
                raise
            except Exception as exc:
                logger.exception(
                    "conclude_investigation_activity transition failed", extra={"error": str(exc)}
                )
                raise _application_failure_from_exc(exc) from exc
        return GenericActivityResult(success=True, data={"status": status})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("conclude_investigation_activity failed", extra={"error": str(exc)})
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def publish_event_activity(
    params: PublishEventInput | dict[str, Any],
) -> GenericActivityResult:
    activity.logger.info("publish_event_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
                logger.warning(
                    "publish_event_activity investigation fetch failed",
                    extra={"error": str(exc)},
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
                    "publish_event_activity outbox enqueue failed", extra={"error": str(exc)}
                )
                raise _application_failure_from_exc(exc) from exc
            if publisher is not None:
                try:
                    await outbox.dispatch_pending(tenant_id, publisher, batch_size=25)
                except Exception as exc:
                    logger.warning(
                        "publish_event_activity dispatch deferred; event stays queued",
                        extra={"error": str(exc)},
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
                    "publish_event_activity fallback failed", extra={"error": str(exc)}
                )
                raise _application_failure_from_exc(exc) from exc

        return GenericActivityResult(success=True, data={"event": event_type})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("publish_event_activity failed", extra={"error": str(exc)})
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def retrieve_knowledge_activity(
    params: RetrieveKnowledgeInput | dict[str, Any],
) -> GenericActivityResult:
    """Validity-gated knowledge retrieval before reasoning (Part 6 D4, Slice 0).

    Returns verified facts + excluded_stale_count. Never raises on store
    trouble: an empty context degrades the investigation to unassisted
    reasoning rather than failing it.
    """
    activity.logger.info("retrieve_knowledge_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
            logger.warning(
                "retrieve_knowledge_activity degraded to empty context",
                extra={"error": str(exc)},
            )
            return GenericActivityResult(
                success=True, data={"verified_facts": [], "excluded_stale_count": -1}
            )
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("retrieve_knowledge_activity failed", extra={"error": str(exc)})
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
    params: CaptureKnowledgeInput | dict[str, Any],
) -> GenericActivityResult:
    """Distill concluded state into envelopes (Part 6 D4, Slice 0).

    Runs after conclusion. Capture failures never fail the investigation.
    """
    activity.logger.info("capture_knowledge_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
            return GenericActivityResult(success=True, data={"artifacts_stored": len(stored)})
        except Exception as exc:
            logger.warning(
                "capture_knowledge_activity failed; investigation unaffected",
                extra={"error": str(exc)},
            )
            return GenericActivityResult(success=True, data={"artifacts_stored": 0})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("capture_knowledge_activity failed", extra={"error": str(exc)})
        raise _application_failure_from_exc(exc) from exc


@dataclass
class SweepKnowledgeInput:
    """One janitor sweep pass over a set of tenants (ISSUE-9)."""

    tenants: list[str] = field(default_factory=list)
    reverify_conditional: bool = False


@activity.defn
async def sweep_knowledge_activity(
    params: SweepKnowledgeInput | dict[str, Any],
) -> GenericActivityResult:
    """Proactive TTL-expiry sweep. Idempotent; never fails a workflow on error."""
    activity.logger.info("sweep_knowledge_activity")
    try:
        from investigation_agent_platform.application.knowledge.janitor import (
            KnowledgeArtifactJanitor,
        )

        data = params if isinstance(params, dict) else params.__dict__
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
                logger.warning(
                    "Knowledge sweep failed for tenant; continuing",
                    extra={"context": {"tenant_id": tenant_id, "error": str(exc)}},
                )
                per_tenant[tenant_id] = -1
        return GenericActivityResult(
            success=True,
            data={"expired_total": total_expired, "per_tenant": per_tenant},
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("sweep_knowledge_activity failed", extra={"error": str(exc)})
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
    params: CollectSnapshotsInput | dict[str, Any],
) -> CollectSnapshotsOutput:
    """Classify one repository's topology snapshots and collect the
    unpinned, out-of-window ones (ISSUE-4 janitor).

    Never deletes a ``TopologySnapshot`` audit node, only the AST subgraph
    beneath it, and never touches anything pinned by an open investigation's
    own recorded evidence. Collection failures are reported, not raised —
    a transient Neo4j hiccup on one repository must not fail the whole
    scheduled pass (the workflow retries this activity per repository).
    """
    activity.logger.info("collect_snapshots_activity")
    data = params if isinstance(params, dict) else params.__dict__
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

        return CollectSnapshotsOutput(
            collected_revisions=collected,
            pinned_count=len(classification.pinned),
            deleted_node_count=deleted_nodes,
            deleted_edge_count=deleted_edges,
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("collect_snapshots_activity failed", extra={"error": str(exc)})
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
    params: RecordInputRequirementInput | dict[str, Any],
) -> GenericActivityResult:
    """Persist one InputRequirement row and return its identity + wait budget."""
    from datetime import timedelta

    from investigation_agent_platform.domain.investigation.input_requirements import (
        AWAITING_INPUT_SOURCES,
        InputRequirement,
    )
    from investigation_agent_platform.domain.investigation.models import InvestigationStatus

    activity.logger.info("record_input_requirement_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
        return GenericActivityResult(success=True, data=result_data)
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("record_input_requirement_activity failed", extra={"error": str(exc)})
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def set_input_requirement_state_activity(
    params: SetInputRequirementStateInput | dict[str, Any],
) -> GenericActivityResult:
    """Compare-and-set a requirement's state (FULFILLED/EXPIRED/CANCELLED)."""
    from investigation_agent_platform.domain.investigation.input_requirements import (
        RequirementState,
    )

    activity.logger.info("set_input_requirement_state_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
        return GenericActivityResult(success=True, data={"state": state.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("set_input_requirement_state_activity failed", extra={"error": str(exc)})
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def promote_fulfillment_evidence_activity(
    params: PromoteFulfillmentEvidenceInput | dict[str, Any],
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

    activity.logger.info("promote_fulfillment_evidence_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
        await ctx.evidence_repo.save(tenant_id, evidence, investigation_id)
        return GenericActivityResult(success=True, data={"evidence_id": str(evidence.evidence_id)})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("promote_fulfillment_evidence_activity failed", extra={"error": str(exc)})
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
    params: RunClusteringInput | dict[str, Any],
) -> GenericActivityResult:
    """Run one bounded incremental clustering pass (Part 11.7)."""
    from investigation_agent_platform.application.finding.clustering_service import (
        FindingClusterService,
        QuotaExceededError,
    )
    from investigation_agent_platform.domain.common.extension import CapabilityScope
    from investigation_agent_platform.domain.common.quotas import QuotaPolicy

    activity.logger.info("run_clustering_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
            logger.warning(
                "Clustering without LLM gateway; lexical-only mode",
                extra={"tenant_id": tenant_id, "error": str(exc)},
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
            logger.warning(
                "Clustering without quota guard; config unavailable",
                extra={"tenant_id": tenant_id, "error": str(exc)},
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
        return GenericActivityResult(success=True, data=result_data)
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("run_clustering_activity failed", extra={"error": str(exc)})
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def update_clustering_job_activity(
    params: UpdateClusteringJobInput | dict[str, Any],
) -> GenericActivityResult:
    """Apply a status/stage/error update to the linked BackgroundJob row."""
    from uuid import UUID as _UUID

    from investigation_agent_platform.domain.common.background_job import (
        BackgroundJobStage,
        BackgroundJobStatus,
    )

    activity.logger.info("update_clustering_job_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
        return GenericActivityResult(success=True, data={"status": updated.status.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("update_clustering_job_activity failed", extra={"error": str(exc)})
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
    params: RunReportInput | dict[str, Any],
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

    activity.logger.info("run_report_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
        refs = await store_report_run(
            store, tenant_id, job_id, html, manifest_json, legal_hold=legal_hold
        )
        await _link_report_result(ctx, tenant_id, job_id, refs["history_html"].key)
        return GenericActivityResult(
            success=True,
            data={
                "taxonomy_revision": report.taxonomy_revision,
                "assignment_count": report.assignment_count,
                "artifact_keys": [refs["history_html"].key, refs["history_json"].key],
            },
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("run_report_activity failed", extra={"error": str(exc)})
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
        logger.warning(
            "Report result_ref linkage skipped",
            extra={"tenant_id": tenant_id, "error": str(exc)},
        )


@activity.defn
async def update_report_job_activity(
    params: UpdateReportJobInput | dict[str, Any],
) -> GenericActivityResult:
    """Apply a status/stage/error update to the linked aggregate-report job."""
    from uuid import UUID as _UUID

    from investigation_agent_platform.domain.common.background_job import (
        BackgroundJobStage,
        BackgroundJobStatus,
    )

    activity.logger.info("update_report_job_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
        return GenericActivityResult(success=True, data={"status": updated.status.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("update_report_job_activity failed", extra={"error": str(exc)})
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
    params: RunReindexInput | dict[str, Any],
) -> GenericActivityResult:
    """Index one reference source into a new generation (Part 11.6)."""
    from investigation_agent_platform.application.reference.reference_service import (
        ReferenceDocumentService,
    )

    activity.logger.info("run_reindex_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
        return GenericActivityResult(success=True, data=summary.model_dump(mode="json"))
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("run_reindex_activity failed", extra={"error": str(exc)})
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def update_reindex_job_activity(
    params: UpdateReindexJobInput | dict[str, Any],
) -> GenericActivityResult:
    """Apply a status/stage/error update to the linked reindex job."""
    from uuid import UUID as _UUID

    from investigation_agent_platform.domain.common.background_job import (
        BackgroundJobStage,
        BackgroundJobStatus,
    )

    activity.logger.info("update_reindex_job_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
        return GenericActivityResult(success=True, data={"status": updated.status.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("update_reindex_job_activity failed", extra={"error": str(exc)})
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
    params: LoadBatchRecordsInput | dict[str, Any],
) -> GenericActivityResult:
    """Load PENDING batch records from a dispatch cursor (Part 11.9)."""
    activity.logger.info("load_batch_records_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
        return GenericActivityResult(success=True, data={"records": pending, "total": total})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("load_batch_records_activity failed", extra={"error": str(exc)})
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def mark_batch_records_activity(
    params: MarkBatchRecordsInput | dict[str, Any],
) -> GenericActivityResult:
    """Bulk-mark records DISPATCHED/RUNNING with child workflow ids."""
    from investigation_agent_platform.domain.intake.batch import BatchRecordStatus

    activity.logger.info("mark_batch_records_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
        return GenericActivityResult(success=True, data={"marked": marked})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("mark_batch_records_activity failed", extra={"error": str(exc)})
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def mark_batch_record_result_activity(
    params: MarkBatchRecordResultInput | dict[str, Any],
) -> GenericActivityResult:
    """Mark one record terminal (DONE/FAILED/CANCELED) with an error."""
    from investigation_agent_platform.domain.intake.batch import BatchRecordStatus

    activity.logger.info("mark_batch_record_result_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
        return GenericActivityResult(success=True, data={"status": status.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("mark_batch_record_result_activity failed", extra={"error": str(exc)})
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def resolve_batch_stragglers_activity(
    params: ResolveBatchStragglersInput | dict[str, Any],
) -> GenericActivityResult:
    """Classify still-running records via the persisted investigation status.

    AWAITING_INPUT investigations keep running (parent completes partial);
    anything else still active stays RUNNING (also left running). Never
    invents terminal states for live executions.
    """
    from investigation_agent_platform.domain.intake.batch import BatchRecordStatus

    activity.logger.info("resolve_batch_stragglers_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
        return GenericActivityResult(
            success=True, data={"awaiting_input": awaiting, "running": running}
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("resolve_batch_stragglers_activity failed", extra={"error": str(exc)})
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def cancel_batch_children_activity(
    params: CancelBatchChildrenInput | dict[str, Any],
) -> GenericActivityResult:
    """Cancel live child executions for RUNNING records (Part 11.9).

    Explicit propagation: the declared parent-close policy is ABANDON (so
    awaiting-input children survive parent completion), therefore parent
    cancellation fans out here — one client cancel per live child, each
    outcome recorded. Failures cancel auditably, never silently.
    """
    from investigation_agent_platform.domain.intake.batch import BatchRecordStatus

    activity.logger.info("cancel_batch_children_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
                failed.append(f"{record.record_index}:{exc}")
        return GenericActivityResult(success=True, data={"canceled": canceled, "failed": failed})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("cancel_batch_children_activity failed", extra={"error": str(exc)})
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def update_batch_job_activity(
    params: UpdateBatchJobInput | dict[str, Any],
) -> GenericActivityResult:
    """Apply a status/stage/error update to the linked batch job."""
    from uuid import UUID as _UUID

    from investigation_agent_platform.domain.common.background_job import (
        BackgroundJobStage,
        BackgroundJobStatus,
    )

    activity.logger.info("update_batch_job_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
        return GenericActivityResult(success=True, data={"status": updated.status.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("update_batch_job_activity failed", extra={"error": str(exc)})
        raise _application_failure_from_exc(exc) from exc


@dataclass
class SummarizeBatchInput:
    tenant_id: str
    job_id: str = ""


@activity.defn
async def summarize_batch_activity(
    params: SummarizeBatchInput | dict[str, Any],
) -> GenericActivityResult:
    """Aggregate batch counts over record rows (Part 11.9)."""
    activity.logger.info("summarize_batch_activity")
    try:
        data = params if isinstance(params, dict) else params.__dict__
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
        return GenericActivityResult(success=True, data=counts)
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("summarize_batch_activity failed", extra={"error": str(exc)})
        raise _application_failure_from_exc(exc) from exc
