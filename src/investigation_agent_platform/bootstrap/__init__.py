"""Bootstrap composition helpers for LLM gateway and application context."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from investigation_agent_platform.api.dependencies import AppContext, set_app_context
from investigation_agent_platform.application.investigation.reasoning import ReasoningCoordinator
from investigation_agent_platform.infrastructure.configuration.config import ApplicationConfig
from investigation_agent_platform.infrastructure.observability.telemetry import (
    OpenTelemetryObservabilityAdapter,
)
from investigation_agent_platform.infrastructure.reasoning.factory import create_llm_gateway
from investigation_agent_platform.ports.observability.telemetry import ObservabilityPort
from investigation_agent_platform.ports.reasoning.llm_gateway import LLMGateway
from investigation_agent_platform.ports.security.redactor import PromptSafetyPolicyPort

logger = logging.getLogger(__name__)


def _run_async(coro: Any) -> Any:
    """Run an awaitable to completion whether or not a loop is already running.

    Mirrors the pattern used for profile seeding in ``api/dependencies.py`` so
    ``build_app_context`` can be called safely from both synchronous module-level
    startup code and from within an already-running event loop (e.g. the
    Temporal worker bootstrap).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    else:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()


class _DefaultPromptSafetyPolicy(PromptSafetyPolicyPort):
    """In-process prompt safety policy — permissive default, production may replace."""

    async def validate_prompt_safety(self, tenant_id: str, prompt_text: str) -> bool:
        return True


def build_reasoning_coordinator(config: ApplicationConfig) -> ReasoningCoordinator:
    """Create ReasoningCoordinator with provider-agnostic LLM gateway."""
    gateway: LLMGateway = create_llm_gateway(config.llm)
    prompt_safety_policy: PromptSafetyPolicyPort = _DefaultPromptSafetyPolicy()
    observability: ObservabilityPort = OpenTelemetryObservabilityAdapter(
        service_name=config.telemetry.service_name,
        otlp_endpoint=config.telemetry.otlp_endpoint,
        enabled=config.telemetry.enabled,
    )
    return ReasoningCoordinator(
        prompt_safety_policy=prompt_safety_policy,
        llm_gateway=gateway,
        observability=observability,
    )


def _build_production_context(config: ApplicationConfig) -> AppContext:
    """Wire real SQLAlchemy, Elastic, Oracle, Git adapters for production."""
    # Validate required URIs
    db_uri = config.database.connection_uri.get_secret_value()
    if not db_uri:
        from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError

        raise PlatformConfigurationError("IAP_DATABASE_URI is required in production")

    engine: AsyncEngine = create_async_engine(
        db_uri,
        pool_size=config.database.pool_size,
        max_overflow=config.database.max_overflow,
        pool_pre_ping=True,
    )

    # Import real repos. Any failure here is a fatal production startup error —
    # production must never silently degrade to in-memory repositories (F-001).
    from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError

    try:
        from investigation_agent_platform.infrastructure.persistence.evidence_repository import (
            SqlAlchemyEvidenceRepository,
        )
        from investigation_agent_platform.infrastructure.persistence.hypothesis_repository import (
            SqlAlchemyHypothesisRepository,
        )
        from investigation_agent_platform.infrastructure.persistence.investigation_repository import (
            SqlAlchemyInvestigationRepository,
        )
        from investigation_agent_platform.infrastructure.persistence.profile_repository import (
            SqlAlchemyApplicationProfileRepository,
        )
        from investigation_agent_platform.infrastructure.persistence.timeline_repository import (
            SqlAlchemyTimelineRepository,
        )

        # session factory is the engine itself for rls_session helper
        session_factory = engine

        investigation_repo = SqlAlchemyInvestigationRepository(session_factory)  # type: ignore[arg-type]
        profile_repo = SqlAlchemyApplicationProfileRepository(session_factory)  # type: ignore[arg-type]
        evidence_repo = SqlAlchemyEvidenceRepository(session_factory)  # type: ignore[arg-type]
        timeline_repo = SqlAlchemyTimelineRepository(session_factory)  # type: ignore[arg-type]
        hypothesis_repo = SqlAlchemyHypothesisRepository(session_factory)  # type: ignore[arg-type]

        from investigation_agent_platform.infrastructure.messaging.outbox import (
            SqlAlchemyOutboxRepository,
        )
        from investigation_agent_platform.infrastructure.persistence.action_execution_repository import (
            SqlAlchemyActionExecutionRepository,
        )
        from investigation_agent_platform.infrastructure.persistence.checkpoint_repository import (
            SqlAlchemyCheckpointRepository,
        )
        from investigation_agent_platform.infrastructure.persistence.idempotency_repository import (
            SqlAlchemyIdempotencyStore,
        )
        from investigation_agent_platform.infrastructure.persistence.transition_repository import (
            SqlAlchemyTransitionRepository,
        )

        checkpoint_repo = SqlAlchemyCheckpointRepository(session_factory)
        transition_repo = SqlAlchemyTransitionRepository(session_factory)
        idempotency_store = SqlAlchemyIdempotencyStore(session_factory)
        action_repo = SqlAlchemyActionExecutionRepository(session_factory)
        outbox_repo = SqlAlchemyOutboxRepository(session_factory)
    except Exception as exc:
        raise PlatformConfigurationError(
            f"Failed to wire production SQLAlchemy repositories: {exc}"
        ) from exc

    # Elastic / Oracle / Git adapters — attach to context if available
    ctx = AppContext(
        investigation_repo=investigation_repo,  # type: ignore[arg-type]
        profile_repo=profile_repo,  # type: ignore[arg-type]
        evidence_repo=evidence_repo,  # type: ignore[arg-type]
        timeline_repo=timeline_repo,  # type: ignore[arg-type]
        hypothesis_repo=hypothesis_repo,  # type: ignore[arg-type]
    )
    # F-011/F-012/F-013: production must never operate on the process-local
    # no-op checkpoint/transition repos or in-memory idempotency store.
    ctx.checkpoint_repo = checkpoint_repo  # type: ignore[attr-defined]
    ctx.transition_repo = transition_repo  # type: ignore[attr-defined]
    ctx.idempotency_store = idempotency_store  # type: ignore[attr-defined]
    ctx.action_repo = action_repo  # type: ignore[attr-defined]
    ctx.outbox_repo = outbox_repo  # type: ignore[attr-defined]

    # Attach engine and observability for health probes. Observability
    # construction failure is fatal in production — it must not silently
    # degrade to an unobserved process.
    ctx.engine = engine  # type: ignore[attr-defined]
    from investigation_agent_platform.infrastructure.observability.telemetry import (
        OpenTelemetryObservabilityAdapter,
    )

    try:
        ctx.observability = OpenTelemetryObservabilityAdapter(  # type: ignore[attr-defined]
            service_name=config.telemetry.service_name,
            otlp_endpoint=config.telemetry.otlp_endpoint,
            enabled=config.telemetry.enabled,
        )
    except Exception as exc:
        raise PlatformConfigurationError(f"Failed to wire observability adapter: {exc}") from exc

    # Event publisher (Kafka) — mandatory in production so domain events are
    # never silently dropped.
    try:
        from faststream.kafka import KafkaBroker

        broker = KafkaBroker(config.kafka.bootstrap_servers)
        ctx.broker = broker  # type: ignore[attr-defined]
        from investigation_agent_platform.infrastructure.messaging.faststream import (
            KafkaEventPublisher,
        )

        ctx.event_publisher = KafkaEventPublisher(broker)  # type: ignore[attr-defined]
    except Exception as exc:
        raise PlatformConfigurationError(f"Failed to wire Kafka event publisher: {exc}") from exc

    # Temporal client — mandatory in production so investigations are never
    # started/dispatched against an absent execution engine.
    try:
        from temporalio.client import Client as TemporalClient

        ctx.temporal_client = _run_async(  # type: ignore[attr-defined]
            TemporalClient.connect(config.temporal.target_host, namespace=config.temporal.namespace)
        )
    except Exception as exc:
        raise PlatformConfigurationError(f"Failed to connect to Temporal: {exc}") from exc

    ctx.temporal_config = config.temporal  # type: ignore[attr-defined]

    return ctx


def build_app_context(config: ApplicationConfig) -> AppContext:
    """Build AppContext with real vs in-memory dependencies based on environment."""
    env = (config.environment or os.environ.get("IAP_ENVIRONMENT", "") or "").lower()
    # Also check explicit env var
    if env == "production" or os.environ.get("IAP_ENVIRONMENT", "").lower() == "production":
        logger.info("Building AppContext with production dependencies", extra={"environment": env})
        ctx = _build_production_context(config)
    else:
        logger.info("Building AppContext with in-memory dependencies", extra={"environment": env})
        ctx = AppContext()
    set_app_context(ctx)
    return ctx


__all__ = ["build_app_context", "build_reasoning_coordinator"]
