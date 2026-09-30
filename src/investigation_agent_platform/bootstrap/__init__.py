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
        # rls_session requires a callable session factory, not the raw
        # engine (passing the engine raises TypeError on first use).
        from sqlalchemy.ext.asyncio import async_sessionmaker

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

        session_factory = async_sessionmaker(engine, expire_on_commit=False)

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
        raise PlatformConfigurationError(f"Failed to wire persistence repositories: {exc}") from exc

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

    _wire_evidence_gateway(ctx, config)
    _wire_topology_dependencies(ctx, config)
    _wire_knowledge_dependencies(ctx, config)
    _wire_graphiti_dependencies(ctx, config)

    return ctx


def _oracle_mcp_servers(config: ApplicationConfig) -> list[dict[str, Any]]:
    """Load Oracle MCP server entries from YAML (no secrets — mounts carry those).

    Missing/empty file means no MCP servers (absent-means-unregistered).
    Entries missing name/command/args/connection are skipped with a warning,
    never half-registered; entries with empty tenants register nothing
    (default-deny) and are logged as such.
    """
    from pathlib import Path

    import yaml

    path = Path(config.evidence.oracle_mcp_config)
    if not path.is_file():
        logger.info(
            "No Oracle MCP server file; skipping MCP registration", extra={"path": str(path)}
        )
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("Oracle MCP server file unreadable; skipping", extra={"error": str(exc)})
        return []
    entries = []
    for entry in raw.get("servers", []) or []:
        if not isinstance(entry, dict):
            continue
        if not all(entry.get(k) for k in ("name", "command", "args", "connection")):
            logger.warning(
                "Oracle MCP server entry incomplete; skipping",
                extra={"entry": str(entry.get("name"))},
            )
            continue
        if not entry.get("tenants"):
            logger.warning(
                "Oracle MCP server entry has no tenants; registering nothing",
                extra={"entry": entry.get("name")},
            )
            continue
        entries.append(entry)
    return entries


def _wire_evidence_gateway(ctx: AppContext, config: ApplicationConfig) -> None:
    """Compose the evidence gateway onto the production context (Part 3 addendum).

    Registers each evidence provider only when its connection details are
    configured (unconfigured providers stay unregistered, never stubbed),
    then builds the gateway with the mandatory sanitizer + query policy and
    tenant-scoped authorizer/registry. Any failure here aborts boot
    (F-001): a half-wired gateway must not silently replace direct paths.
    """
    from investigation_agent_platform.application.evidence.gateway import AsyncEvidenceGateway
    from investigation_agent_platform.application.evidence.selector import (
        EvidenceProviderSelector,
    )
    from investigation_agent_platform.application.investigation.composition import (
        build_investigation_services,
    )
    from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError
    from investigation_agent_platform.infrastructure.configuration.mapping_registry import (
        load_mapping_profiles,
    )
    from investigation_agent_platform.infrastructure.evidence.security import (
        QuerySafetyPolicy,
        SensitiveDataRedactor,
    )
    from investigation_agent_platform.infrastructure.topology.profile_repository_registry import (
        ProfileBackedRepositoryRegistry,
    )

    try:
        # Part 10: operator-authored field contracts. Load failure aborts
        # boot (F-001) — a half-mapped evidence layer must never serve.
        mapping_registry = load_mapping_profiles(config.evidence.mapping_profiles_path)
        logger.info(
            "Observability mappings loaded",
            extra={"source_ids": sorted(mapping_registry.source_ids)},
        )
        selector = EvidenceProviderSelector()
        if config.evidence.elasticsearch_url:
            from elasticsearch import AsyncElasticsearch

            from investigation_agent_platform.infrastructure.evidence.runtime.elastic import (
                AsyncElasticAdapter,
                ElasticAdapterSettings,
            )

            selector.register_runtime_provider(
                "elastic-primary",
                AsyncElasticAdapter(
                    client=AsyncElasticsearch(
                        config.evidence.elasticsearch_url,
                        request_timeout=config.evidence.elastic_request_timeout,
                    ),
                    settings=ElasticAdapterSettings.from_evidence_config(config.evidence),
                    provider_id="elastic-primary",
                    request_timeout=config.evidence.elastic_request_timeout,
                    mapping_registry=mapping_registry,
                ),
                # Default route: one shared Elastic deployment serves all
                # tenant/app/env contexts (per-request scoping in the adapter).
                default=True,
            )
        else:
            logger.info("Elastic provider unconfigured; key elastic-primary stays unregistered")
        if config.evidence.oracle_dsn.get_secret_value():
            import importlib.util

            if importlib.util.find_spec("oracledb") is None:
                logger.warning("oracledb package absent; key oracle-primary stays unregistered")
            else:
                from sqlalchemy.ext.asyncio import create_async_engine as _create_engine

                from investigation_agent_platform.infrastructure.evidence.state.oracle import (
                    AsyncOracleStateAdapter,
                    SqlglotTemplateValidator,
                )

                selector.register_state_provider(
                    "oracle-primary",
                    AsyncOracleStateAdapter(
                        engine=_create_engine(
                            f"oracle+oracledb://{config.evidence.oracle_dsn.get_secret_value()}"
                        ),
                        template_repository={},
                        # Empty allowlist = no table restriction at the
                        # validator layer (SELECT-only + bounds still
                        # enforced); per-profile templates scope queries.
                        validator=SqlglotTemplateValidator(set()),
                        provider_id="oracle-primary",
                        query_timeout=config.evidence.oracle_query_timeout,
                    ),
                )
        else:
            logger.info("Oracle DSN unconfigured; key oracle-primary stays unregistered")
        if config.topology.code_repo_base_path:
            from investigation_agent_platform.infrastructure.evidence.code.git import (
                PyGit2Adapter,
            )

            selector.register_code_provider(
                "pygit2-local",
                PyGit2Adapter(
                    repo_base_path=config.topology.code_repo_base_path,
                    provider_id="pygit2-local",
                ),
            )
        # MCP registers only with explicit server entries (no config shape
        # means unregistered, per the addendum). One adapter per database.
        for entry in _oracle_mcp_servers(config):
            from investigation_agent_platform.infrastructure.evidence.mcp import (
                McpEvidenceAdapter,
            )

            allowlist = {t: {entry["connection"]} for t in entry.get("tenants", [])}
            selector.register_state_provider(
                f"oracle-mcp-{entry['name']}",
                McpEvidenceAdapter(
                    server_params={"command": entry["command"], "args": entry["args"]},
                    allowed_tools=entry.get("allowed_tools", ["run-sql"]),
                    tool_timeout_seconds=float(entry.get("timeout_seconds", 30.0)),
                    max_items_per_call=int(entry.get("max_rows", 1000)),
                    connection_name=entry.get("connection", ""),
                    tenant_allowlist=allowlist or None,
                ),
            )
            logger.info(
                "MCP Oracle server registered", extra={"key": f"oracle-mcp-{entry['name']}"}
            )
        selector.freeze()

        from investigation_agent_platform.infrastructure.security.profile_authorizer import (
            ProfileBasedActionAuthorizer,
            ProfileBasedCapabilityRegistry,
        )

        sanitizer = SensitiveDataRedactor()
        ctx.evidence_gateway = AsyncEvidenceGateway(  # type: ignore[attr-defined]
            provider_selector=selector,
            query_safety_policy=QuerySafetyPolicy(),
            sanitizer=sanitizer,
            telemetry=getattr(ctx, "observability", None),
            timeout_seconds=config.evidence.gateway_timeout_seconds,
            authorizer=ProfileBasedActionAuthorizer(ctx.profile_repo),
            capability_registry=ProfileBasedCapabilityRegistry(ctx.profile_repo),
            evidence_repository=ctx.evidence_repo,
            repository_registry=ProfileBackedRepositoryRegistry(ctx.profile_repo),
        )
        logger.info("Evidence gateway composed")

        from investigation_agent_platform.infrastructure.evidence.logs.trace_telemetry import (
            TraceTelemetryExtractor,
        )

        # Part 9: transport-independent investigation services over the same
        # gateway/selector/repositories (shared by MCP, REST, CLI, workers).
        ctx.investigation_services = build_investigation_services(  # type: ignore[attr-defined]
            gateway=ctx.evidence_gateway,  # type: ignore[attr-defined]
            selector=selector,
            investigation_repo=ctx.investigation_repo,
            evidence_repo=ctx.evidence_repo,
            profile_repo=ctx.profile_repo,
            sanitizer=sanitizer,
            derivation=TraceTelemetryExtractor(mapping_registry=mapping_registry),
        )
        logger.info("Investigation services composed")
    except Exception as exc:
        raise PlatformConfigurationError(f"Failed to wire evidence gateway: {exc}") from exc


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
        trace_hop_resolver=_hop_resolver_for(ctx),
    )
    logger.info("Layer 3 topology backend (Neo4j) wired successfully")


def _hop_resolver_for(ctx: AppContext) -> Any | None:
    """Build the trace-hop resolver when a gateway is composed, else None.

    Absent resolver keeps single-repository attribution (fail-closed); this
    is the normal dev state, never an error.
    """
    gateway = getattr(ctx, "evidence_gateway", None)
    if gateway is None:
        logger.info("No evidence gateway composed; cross-repo hopping stays disabled")
        return None
    from investigation_agent_platform.infrastructure.topology.trace_hop import (
        GatewayTraceHopResolver,
    )

    return GatewayTraceHopResolver(gateway)


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
        # Best-effort Temporal wiring outside production: when the engine is
        # reachable (e.g. local docker-compose), attach a client so workflow
        # dispatch (/start) and signals work in dev. When unreachable, leave
        # it absent — routers report 503 and readiness reports NOT_REQUIRED.
        try:
            from temporalio.client import Client as _TemporalClient

            ctx.temporal_client = _run_async(  # type: ignore[attr-defined]
                _TemporalClient.connect(
                    config.temporal.target_host, namespace=config.temporal.namespace
                )
            )
            ctx.temporal_config = config.temporal  # type: ignore[attr-defined]
            logger.info(
                "Wired dev Temporal client",
                extra={"target": config.temporal.target_host},
            )
        except Exception as exc:
            logger.warning(
                "Temporal unavailable; workflow dispatch disabled in this process",
                extra={"error": str(exc)},
            )
    set_app_context(ctx)
    return ctx


__all__ = ["build_app_context", "build_reasoning_coordinator"]
