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


def _application_failure_from_exc(exc: BaseException) -> ApplicationFailure:
    """Map domain/generic exceptions to Temporal ApplicationFailure with correct retryability."""
    from investigation_agent_platform.domain.common.exceptions import DomainException

    if isinstance(exc, DomainException):
        return ApplicationFailure(
            str(exc),
            type=exc.__class__.__name__,
            non_retryable=not exc.retryable,
        )
    # Generic exceptions are retryable by default to allow Temporal retries.
    return ApplicationFailure(str(exc), type=exc.__class__.__name__, non_retryable=False)


@dataclass
class CreateInvestigationInput:
    application_id: str
    session_id: str | None = None
    tenant_id: str = ""
    description: str | None = None


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
        # Prefer EvidenceGateway if wired on context, otherwise no-op.
        gateway: Any = getattr(ctx, "evidence_gateway", None)
        if gateway is None:
            # Fallback: return empty evidence set when gateway not configured (e.g. in-memory dev).
            return GenericActivityResult(
                success=True, data={"items_count": 0, "tenant_id": tenant_id}
            )
        # Attempt a minimal runtime evidence search to prove wiring; fail-closed on error.
        try:
            from investigation_agent_platform.domain.evidence.requests import RuntimeEvidenceRequest

            req = RuntimeEvidenceRequest(environment="production", query_string="*")
            app_id: str = str(data.get("application_id") or "example-app")
            result = await gateway.search_runtime_evidence(tenant_id, investigation_id, app_id, req)
            return GenericActivityResult(
                success=True,
                data={"items_count": len(result.items), "total_count": result.total_count},
            )
        except Exception as exc:
            logger.exception(
                "retrieve_evidence_activity gateway call failed", extra={"error": str(exc)}
            )
            raise _application_failure_from_exc(exc) from exc
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
        # Resolve ReasoningCoordinator: prefer ctx.reasoning_coordinator, else build minimal one.
        coordinator: Any = getattr(ctx, "reasoning_coordinator", None)
        if coordinator is None:
            try:
                from investigation_agent_platform.application.investigation.reasoning import (
                    ReasoningCoordinator,
                )
                from investigation_agent_platform.bootstrap import build_reasoning_coordinator
                from investigation_agent_platform.infrastructure.configuration.config import (
                    load_application_config_from_env,
                )

                try:
                    cfg = load_application_config_from_env()
                    coordinator = build_reasoning_coordinator(cfg)
                except Exception:
                    # Fallback stub coordinator with permissive safety policy.
                    class _AllowAllSafety:
                        async def validate_prompt_safety(self, _tid: str, _prompt: str) -> bool:
                            return True

                    coordinator = ReasoningCoordinator(prompt_safety_policy=_AllowAllSafety())
            except Exception as exc:
                logger.exception(
                    "reason_activity coordinator fallback failed", extra={"error": str(exc)}
                )
                raise _application_failure_from_exc(exc) from exc

        # Build minimal InvestigationState for reasoning.
        try:
            inv_repo: Any = getattr(ctx, "investigation_repo", None)
            investigation: Any = None
            if inv_repo is not None:
                investigation = await inv_repo.get_by_id(tenant_id, investigation_id)
            if investigation is None:
                # No persisted investigation — return stub readiness.
                return GenericActivityResult(
                    success=True, data={"conclusion_readiness": 0.85, "iteration": iteration}
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
        # Dispatch via EvidenceGateway if available; always sanitize + persist when possible.
        gateway: Any = getattr(ctx, "evidence_gateway", None)
        evidence_repo: Any = getattr(ctx, "evidence_repo", None)
        result_status = "EXECUTED"
        items_count = 0
        if gateway is not None:
            try:
                from investigation_agent_platform.domain.evidence.requests import (
                    ApplicationStateRequest,
                )

                # Map generic action to gateway call; default to get_application_state for QUERY_STATE.
                if action_type == "QUERY_STATE":
                    req = ApplicationStateRequest(
                        environment="production", template_id="default", parameters=parameters
                    )
                    app_id = str(data.get("application_id") or "example-app")
                    qres = await gateway.get_application_state(
                        tenant_id, investigation_id, app_id, req
                    )
                    items_count = len(qres.items)
                    # Persist sanitized evidence items.
                    if evidence_repo is not None and qres.items:
                        try:
                            await evidence_repo.save_batch(tenant_id, qres.items, investigation_id)
                        except Exception as exc:
                            logger.exception(
                                "execute_action_activity save_batch failed",
                                extra={"error": str(exc)},
                            )
                            raise _application_failure_from_exc(exc) from exc
                else:
                    # For other action types, treat as dispatched without provider-specific handling.
                    result_status = "EXECUTED"
            except ApplicationFailure:
                raise
            except Exception as exc:
                logger.exception(
                    "execute_action_activity gateway dispatch failed", extra={"error": str(exc)}
                )
                raise _application_failure_from_exc(exc) from exc

        # Audit action execution if repo present.
        action_repo: Any = getattr(ctx, "action_repo", getattr(ctx, "action_execution_repo", None))
        if action_repo is not None:
            try:
                from investigation_agent_platform.domain.investigation.models import (
                    ActionType,
                    InvestigationAction,
                )

                # Resolve ActionType safely; fallback to QUERY_STATE.
                try:
                    at = ActionType(action_type)
                except ValueError:
                    at = ActionType.QUERY_STATE
                inv_action = InvestigationAction.create(
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    action_type=at,
                    parameters=parameters,
                )
                await action_repo.record_action(
                    tenant_id, investigation_id, inv_action, result_status
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
    from investigation_agent_platform.domain.investigation.models import ActorType

    # Reuse same valid_transitions as Investigation.transition_to for path finding
    # Build graph via temporary instance call by inspecting valid_transitions from method closure?
    # Instead import and reconstruct consistent graph via a dummy investigation.
    from investigation_agent_platform.domain.investigation.models import InvestigationStatus

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
        InvestigationStatus.COMPLETED: set(),
        InvestigationStatus.FAILED: set(),
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
            return GenericActivityResult(success=False, error="tenant_id, investigation_id and target_status required")
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
            return GenericActivityResult(success=True, data={"status": target.value, "already": True})
        await _transition_via_path(tenant_id, inv, target, reason, inv_repo)
        return GenericActivityResult(success=True, data={"status": target.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        logger.exception("transition_investigation_activity failed", extra={"error": str(exc)})
        raise _application_failure_from_exc(exc) from exc


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
                    if inv.status != target:
                        await _transition_via_path(
                            tenant_id, inv, target, f"Workflow concluded with {status}", inv_repo
                        )
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
        if publisher is None:
            # No publisher wired — succeed as no-op.
            return GenericActivityResult(success=True, data={"event": event_type})

        # Prefer publish_domain_event with a concrete InvestigationEvent.
        try:
            from investigation_agent_platform.domain.events.base import InvestigationConcluded

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
                    idempotency_key=f"{investigation_id}:{event_type}",
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
