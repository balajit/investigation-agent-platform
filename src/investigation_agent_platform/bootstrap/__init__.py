"""Bootstrap composition helpers for LLM gateway and application context."""

from __future__ import annotations

import logging
import os

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

    # Import real repos lazily to avoid hard dependency in dev
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
    except Exception as exc:
        logger.warning("Failed to wire SQLAlchemy repos, falling back to in-memory: %s", exc)
        return AppContext()

    # Elastic / Oracle / Git adapters — attach to context if available
    ctx = AppContext(
        investigation_repo=investigation_repo,  # type: ignore[arg-type]
        profile_repo=profile_repo,  # type: ignore[arg-type]
        evidence_repo=evidence_repo,  # type: ignore[arg-type]
        timeline_repo=timeline_repo,  # type: ignore[arg-type]
        hypothesis_repo=hypothesis_repo,  # type: ignore[arg-type]
    )

    # Attach engine and observability for health probes
    ctx.engine = engine  # type: ignore[attr-defined]
    try:
        from investigation_agent_platform.infrastructure.observability.telemetry import (
            OpenTelemetryObservabilityAdapter,
        )

        ctx.observability = OpenTelemetryObservabilityAdapter(  # type: ignore[attr-defined]
            service_name=config.telemetry.service_name,
            otlp_endpoint=config.telemetry.otlp_endpoint,
            enabled=config.telemetry.enabled,
        )
    except Exception:
        pass

    # Event publisher (Kafka) if configured
    try:
        from faststream.kafka import KafkaBroker

        broker = KafkaBroker(config.kafka.bootstrap_servers)
        ctx.broker = broker  # type: ignore[attr-defined]
        from investigation_agent_platform.infrastructure.messaging.faststream import (
            KafkaEventPublisher,
        )

        ctx.event_publisher = KafkaEventPublisher(broker)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.info("Kafka publisher not wired: %s", exc)

    # Temporal config for health probe
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
