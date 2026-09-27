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
    activity.logger.info("create_investigation_activity", extra={"app_id": params.application_id})
    try:
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
                max_results = min(profile.investigation_configuration.max_evidence_per_query, 500)

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

            req = RuntimeEvidenceRequest(
                environment=environment,
                keywords=list(parameters.get("keywords", [])) or [],
                services=list(parameters.get("services", [])) or [],
                limit=int(parameters.get("limit", 100)),
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
    blockers are persisted in the lifecycle rather than swallowed.
    """
    from investigation_agent_platform.application.investigation.verification import (
        ConclusionGate,
        RootCauseVerificationPolicy,
        VerificationEngine,
    )
    from investigation_agent_platform.domain.investigation.models import InvestigationStatus

    evidence_repo: Any = getattr(ctx, "evidence_repo", None)
    hypothesis_repo: Any = getattr(ctx, "hypothesis_repo", None)
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
    if decision.approved:
        return InvestigationStatus.COMPLETED
    logger.warning(
        "ConclusionGate denied COMPLETED: %s",
        "; ".join(decision.blockers),
        extra={"tenant_id": tenant_id, "investigation_id": str(investigation_id)},
    )
    return InvestigationStatus.FAILED


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
                result = await janitor.sweep_tenant(
                    tenant_id, reverify_conditional=reverify
                )
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
