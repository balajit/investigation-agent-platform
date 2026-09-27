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

        from investigation_agent_platform.infrastructure.persistence.knowledge_repository import (
            SqlAlchemyArtifactRepository,
            SqlAlchemyCodeIssueIndex,
            SqlAlchemySessionRepository,
        )

        artifact_repo = SqlAlchemyArtifactRepository(session_factory)
        session_repo = SqlAlchemySessionRepository(session_factory)
        code_issue_index = SqlAlchemyCodeIssueIndex(session_factory)
    except Exception as exc:
        raise PlatformConfigurationError(
            f"Failed to wire Mem0 knowledge store: {exc}"
        ) from exc


def _wire_graphiti_dependencies(ctx: AppContext, config: ApplicationConfig) -> None:
    """Wire Part 6 Slice 2 Graphiti temporal projection.

    Builds GraphitiTemporalKnowledge against Neo4j when
    `config.knowledge.graphiti_enabled` is true. Neo4j connection fields
    fall back to the Layer 3 topology settings so both graph consumers
    share one instance by default. Any wiring failure aborts production
    startup (F-001 precedent); runtime projection failures later only
    degrade to envelopes.
    """
    from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError

    if not config.knowledge.graphiti_enabled:
        logger.info("Graphiti projection disabled; envelopes only")
        return

    from investigation_agent_platform.infrastructure.knowledge.graphiti_adapter import (
        GraphitiTemporalKnowledge,
    )

    try:
        uri = config.knowledge.graphiti_neo4j_uri.get_secret_value()
        user = config.knowledge.graphiti_neo4j_user.get_secret_value()
        password = config.knowledge.graphiti_neo4j_password.get_secret_value()
        if not uri:
            uri = config.topology.uri.get_secret_value()
            user = config.topology.username.get_secret_value()
            password = config.topology.password.get_secret_value()
        ctx.temporal_port = GraphitiTemporalKnowledge(  # type: ignore[attr-defined]
            neo4j_uri=uri,
            neo4j_user=user,
            neo4j_password=password,
            neo4j_database=config.knowledge.graphiti_neo4j_database,
            model=config.knowledge.graphiti_model,
            api_key=config.llm.api_key.get_secret_value(),
            embedder_model=config.knowledge.graphiti_embedder_model,
            semaphore_limit=config.knowledge.graphiti_semaphore_limit,
        )
        # Graph-schema migration step (mirrors Layer 3 install_constraints):
        # create Graphiti indexes/constraints once at startup, never per request.
        _run_async(ctx.temporal_port.ensure_indices())  # type: ignore[attr-defined]
        logger.info("Graphiti temporal projection wired")
    except Exception as exc:
        raise PlatformConfigurationError(
            f"Failed to wire Graphiti temporal knowledge: {exc}"
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
    # Part 6 Slice 0: durable knowledge stores (RLS-scoped envelopes/sessions;
    # tenant-free coordination index by construction).
    ctx.artifact_repo = artifact_repo  # type: ignore[attr-defined]
    ctx.session_repo = session_repo  # type: ignore[attr-defined]
    ctx.code_issue_index = code_issue_index  # type: ignore[attr-defined]

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

    _wire_topology_dependencies(ctx, config)
    _wire_knowledge_dependencies(ctx, config)
    _wire_graphiti_dependencies(ctx, config)

    return ctx


def _wire_knowledge_dependencies(ctx: AppContext, config: ApplicationConfig) -> None:
    """Wire Part 6 knowledge adapters onto an already-constructed AppContext.

    Slice 1 (Mem0): builds Mem0KnowledgeStore against pgvector when
    `config.knowledge.mem0_enabled` is true. Any construction failure aborts
    production startup (F-001 precedent) instead of silently running without
    the projection — but a *runtime* projection failure later only degrades
    to envelopes (see capture/retrieval services).
    """
    from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError

    if not config.knowledge.mem0_enabled:
        logger.info("Mem0 projection disabled; envelopes only")
        return

    from investigation_agent_platform.infrastructure.knowledge.mem0_adapter import (
        Mem0KnowledgeStore,
    )

    try:
        pgvector_url = config.knowledge.mem0_pgvector_url.get_secret_value()
        if not pgvector_url:
            pgvector_url = config.database.connection_uri.get_secret_value()
        ctx.knowledge_store = Mem0KnowledgeStore(  # type: ignore[attr-defined]
            model=config.knowledge.mem0_model,
            api_key=config.llm.api_key.get_secret_value(),
            embedder_model=config.knowledge.mem0_embedder_model,
            embedding_dims=config.knowledge.mem0_embedding_dims,
            collection_name=config.knowledge.mem0_collection,
            pgvector_url=pgvector_url,
        )
        logger.info("Mem0 knowledge projection wired")
    except Exception as exc:
        raise PlatformConfigurationError(f"Failed to wire Mem0 knowledge store: {exc}") from exc


def _wire_topology_dependencies(ctx: AppContext, config: ApplicationConfig) -> None:
    """Wire Layer 3 topology dependencies onto an already-constructed AppContext.

    Overrides the in-memory topology adapter (set unconditionally in
    ``AppContext.__init__`` for dev/test) with the Neo4j-backed adapter when
    ``config.topology.enabled`` is true. If ``config.topology.required`` is
    also true, any wiring/connectivity failure aborts production startup
    (F-001 precedent) instead of silently falling back to the in-memory
    adapter.
    """
    from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError

    if not config.topology.enabled:
        logger.info("Layer 3 topology is disabled; using in-memory adapter (dev/test only)")
        return

    from investigation_agent_platform.application.topology.attribution import (
        FailureAttributionService,
    )
    from investigation_agent_platform.infrastructure.topology.neo4j_adapter import (
        Neo4jTopologyAdapter,
    )
    from investigation_agent_platform.infrastructure.topology.profile_repository_registry import (
        ProfileBackedRepositoryRegistry,
    )

    try:
        adapter = Neo4jTopologyAdapter(config.topology)
        _run_async(adapter.connect())
    except Exception as exc:
        if config.topology.required:
            raise PlatformConfigurationError(
                f"Failed to connect to Neo4j topology backend: {exc}"
            ) from exc
        logger.warning(
            "Layer 3 topology backend unavailable and not required; "
            "continuing with in-memory adapter",
            extra={"error": str(exc)},
        )
        return

    ctx.topology_adapter = adapter  # type: ignore[attr-defined]
    ctx.topology_config = config.topology  # type: ignore[attr-defined]
    ctx.repository_registry = ProfileBackedRepositoryRegistry(  # type: ignore[attr-defined]
        ctx.profile_repo,
        ownership_registry=adapter,
        repo_base_path=config.topology.code_repo_base_path,
    )
    # ISSUE-6/ISSUE-3 graceful tiers: resolvers degrade gracefully when the
    # base path is unconfigured (every resolve returns None + debug log).
    from investigation_agent_platform.infrastructure.evidence.code.codeowners import (
        CodeownersResolver,
    )
    from investigation_agent_platform.infrastructure.evidence.code.micro import (
        MicroSymbolResolver,
    )

    codeowners_resolver = CodeownersResolver(
        repo_base_path=config.topology.code_repo_base_path,
    )
    micro_resolver = MicroSymbolResolver(
        repo_base_path=config.topology.code_repo_base_path,
    )
    ctx.failure_attribution_service = FailureAttributionService(  # type: ignore[attr-defined]
        attribution_port=adapter,
        evidence_repo=ctx.evidence_repo,
        repository_registry=ctx.repository_registry,
        codeowners_resolver=codeowners_resolver,
        micro_resolver=micro_resolver,
    )
    logger.info("Layer 3 topology backend (Neo4j) wired successfully")


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
