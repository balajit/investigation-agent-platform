# src/investigation_agent_platform/bootstrap/buffered_dev.py
"""Dev-only buffered persistence wiring (never production).

Outside production, repository writes should land in Postgres whenever it is
reachable and journal to a local SQLite WAL otherwise (see
``infrastructure.persistence.buffered``). This module builds that composition:

- Probes Postgres with a short timeout. Reachable → real SQLAlchemy stores
  as proxy primaries. Unreachable → ``DownPrimary`` stubs (every call takes
  the buffer path).
- Fallbacks are the ``AppContext``'s own in-memory stores, so any state
  already in them is preserved and served during outages.
- Unflushed WAL entries not yet applied to fallbacks are restored on startup
  (restart recovery for the read side).
- A background flusher replays the WAL into Postgres when it is reachable;
  conflicts go HELD + loud, never overwritten.

Production is untouched: ``ensure_buffered_dev_context`` refuses to run when
``IAP_ENVIRONMENT=production`` (F-001 fail-fast stays intact).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

BUFFERED_ATTRS: tuple[str, ...] = (
    "investigation_repo",
    "profile_repo",
    "evidence_repo",
    "timeline_repo",
    "hypothesis_repo",
    "input_repo",
    "finding_cluster_repo",
    "chat_repo",
    "reference_repo",
    "batch_repo",
    "background_job_repo",
    "finding_repo",
    "checkpoint_repo",
    "transition_repo",
    "action_repo",
    "outbox_repo",
    "idempotency_store",
    "artifact_repo",
    "session_repo",
    "code_issue_index",
)


def _async_db_uri(db_uri: str) -> str:
    """Map a sync ``postgresql://`` URI to its async driver equivalent.

    Operator env files carry the sync scheme (alembic, psql); SQLAlchemy's
    asyncio extension needs ``postgresql+asyncpg://``. Async schemes pass
    through untouched.
    """
    if db_uri.startswith("postgresql://"):
        return "postgresql+asyncpg://" + db_uri[len("postgresql://") :]
    if db_uri.startswith("postgres://"):
        return "postgresql+asyncpg://" + db_uri[len("postgres://") :]
    return db_uri


async def _postgres_reachable(db_uri: str, timeout_seconds: float = 5.0) -> bool:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(_async_db_uri(db_uri), pool_pre_ping=True)
    try:
        async with asyncio.timeout(timeout_seconds):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.warning(
            "Postgres unreachable; buffered persistence starts in fallback mode",
            extra={"error": str(exc)},
        )
        return False
    finally:
        await engine.dispose()


def _build_sqlalchemy_stores(session_factory: Any) -> dict[str, Any]:
    from investigation_agent_platform.infrastructure.messaging.outbox import (
        SqlAlchemyOutboxRepository,
    )
    from investigation_agent_platform.infrastructure.persistence.action_execution_repository import (
        SqlAlchemyActionExecutionRepository,
    )
    from investigation_agent_platform.infrastructure.persistence.background_job_repository import (
        SqlAlchemyBackgroundJobRepository,
    )
    from investigation_agent_platform.infrastructure.persistence.batch_intake_repository import (
        SqlAlchemyBatchIntakeRepository,
    )
    from investigation_agent_platform.infrastructure.persistence.chat_repository import (
        SqlAlchemyChatSessionRepository,
    )
    from investigation_agent_platform.infrastructure.persistence.checkpoint_repository import (
        SqlAlchemyCheckpointRepository,
    )
    from investigation_agent_platform.infrastructure.persistence.evidence_repository import (
        SqlAlchemyEvidenceRepository,
    )
    from investigation_agent_platform.infrastructure.persistence.finding_cluster_repository import (
        SqlAlchemyFindingClusterRepository,
    )
    from investigation_agent_platform.infrastructure.persistence.finding_repository import (
        SqlAlchemyFindingConclusionRepository,
    )
    from investigation_agent_platform.infrastructure.persistence.hypothesis_repository import (
        SqlAlchemyHypothesisRepository,
    )
    from investigation_agent_platform.infrastructure.persistence.idempotency_repository import (
        SqlAlchemyIdempotencyStore,
    )
    from investigation_agent_platform.infrastructure.persistence.input_requirement_repository import (
        SqlAlchemyInputRequirementRepository,
    )
    from investigation_agent_platform.infrastructure.persistence.investigation_repository import (
        SqlAlchemyInvestigationRepository,
    )
    from investigation_agent_platform.infrastructure.persistence.knowledge_repository import (
        SqlAlchemyArtifactRepository,
        SqlAlchemyCodeIssueIndex,
        SqlAlchemySessionRepository,
    )
    from investigation_agent_platform.infrastructure.persistence.profile_repository import (
        SqlAlchemyApplicationProfileRepository,
    )
    from investigation_agent_platform.infrastructure.persistence.reference_doc_repository import (
        SqlAlchemyReferenceDocRepository,
    )
    from investigation_agent_platform.infrastructure.persistence.timeline_repository import (
        SqlAlchemyTimelineRepository,
    )
    from investigation_agent_platform.infrastructure.persistence.transition_repository import (
        SqlAlchemyTransitionRepository,
    )

    return {
        "investigation_repo": SqlAlchemyInvestigationRepository(session_factory),
        "profile_repo": SqlAlchemyApplicationProfileRepository(session_factory),
        "evidence_repo": SqlAlchemyEvidenceRepository(session_factory),
        "timeline_repo": SqlAlchemyTimelineRepository(session_factory),
        "hypothesis_repo": SqlAlchemyHypothesisRepository(session_factory),
        "input_repo": SqlAlchemyInputRequirementRepository(session_factory),
        "finding_cluster_repo": SqlAlchemyFindingClusterRepository(session_factory),
        "chat_repo": SqlAlchemyChatSessionRepository(session_factory),
        "reference_repo": SqlAlchemyReferenceDocRepository(session_factory),
        "batch_repo": SqlAlchemyBatchIntakeRepository(session_factory),
        "background_job_repo": SqlAlchemyBackgroundJobRepository(session_factory),
        "finding_repo": SqlAlchemyFindingConclusionRepository(session_factory),
        "checkpoint_repo": SqlAlchemyCheckpointRepository(session_factory),
        "transition_repo": SqlAlchemyTransitionRepository(session_factory),
        "action_repo": SqlAlchemyActionExecutionRepository(session_factory),
        "outbox_repo": SqlAlchemyOutboxRepository(session_factory),
        "idempotency_store": SqlAlchemyIdempotencyStore(session_factory),
        "artifact_repo": SqlAlchemyArtifactRepository(session_factory),
        "session_repo": SqlAlchemySessionRepository(session_factory),
        "code_issue_index": SqlAlchemyCodeIssueIndex(session_factory),
    }


def _require_dev() -> None:
    if os.environ.get("IAP_ENVIRONMENT", "development").lower() == "production":
        from investigation_agent_platform.domain.common.exceptions import (
            PlatformConfigurationError,
        )

        raise PlatformConfigurationError(
            "Buffered persistence is dev-only; production must fail fast (F-001)"
        )


async def ensure_buffered_dev_context(ctx: Any, config: Any) -> Any:
    """Wrap a live ``AppContext``'s stores with buffered proxies (idempotent).

    Must be called from a running event loop (lifespan). Never raises: on any
    unexpected failure the context is left untouched so startup proceeds.
    """
    from investigation_agent_platform.infrastructure.persistence.buffered import (
        BufferedProxy,
        DownPrimary,
        SqliteWal,
        restore_fallbacks,
    )

    if getattr(ctx, "_buffered", False):
        return ctx
    try:
        _require_dev()
        buffer_cfg = getattr(config, "buffer", None)
        if buffer_cfg is not None and not getattr(buffer_cfg, "enabled", True):
            logger.info("Buffered persistence disabled (IAP_BUFFER_ENABLED=false)")
            return ctx
        path = getattr(buffer_cfg, "path", "data/iap-buffer.sqlite3") or ("data/iap-buffer.sqlite3")
        interval = float(getattr(buffer_cfg, "flush_interval_seconds", 5.0) or 5.0)

        db_uri = config.database.connection_uri.get_secret_value()
        reachable = await _postgres_reachable(db_uri) if db_uri else False

        engine = None
        primaries: dict[str, Any] = {}
        if reachable:
            from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

            engine = create_async_engine(
                _async_db_uri(db_uri),
                pool_size=config.database.pool_size,
                max_overflow=config.database.max_overflow,
                pool_pre_ping=True,
            )
            primaries = _build_sqlalchemy_stores(async_sessionmaker(engine, expire_on_commit=False))
            logger.info("Buffered persistence: Postgres primary wired")
        else:
            primaries = {name: DownPrimary(name) for name in BUFFERED_ATTRS}
            logger.warning("Buffered persistence: starting without primary; writes journal to WAL")

        wal = SqliteWal(path)
        proxies: dict[str, BufferedProxy] = {}
        for name in BUFFERED_ATTRS:
            fallback = getattr(ctx, name, None)
            if fallback is None:
                continue
            proxy = BufferedProxy(primaries[name], fallback, wal, name)
            setattr(ctx, name, proxy)
            proxies[name] = proxy

        restored = await restore_fallbacks(proxies, wal)
        if restored:
            logger.info(
                "Restored buffered writes into fallback stores", extra={"restored": restored}
            )

        # Rebuild services that captured bare repo references at construction.
        from investigation_agent_platform.api.dependencies import InvestigationOutboxWorker
        from investigation_agent_platform.application.topology.attribution import (
            FailureAttributionService,
        )
        from investigation_agent_platform.infrastructure.evidence.code.codeowners import (
            CodeownersResolver,
        )
        from investigation_agent_platform.infrastructure.evidence.code.micro import (
            MicroSymbolResolver,
        )
        from investigation_agent_platform.infrastructure.security.profile_authorizer import (
            ProfileBasedActionAuthorizer,
            ProfileBasedCapabilityRegistry,
        )
        from investigation_agent_platform.infrastructure.topology.profile_repository_registry import (
            ProfileBackedRepositoryRegistry,
        )

        ctx.action_authorizer = ProfileBasedActionAuthorizer(ctx.profile_repo)
        ctx.capability_registry = ProfileBasedCapabilityRegistry(ctx.profile_repo)
        ctx.repository_registry = ProfileBackedRepositoryRegistry(ctx.profile_repo)
        ctx.failure_attribution_service = FailureAttributionService(
            attribution_port=ctx.topology_adapter,
            evidence_repo=ctx.evidence_repo,
            repository_registry=ctx.repository_registry,
            codeowners_resolver=CodeownersResolver(
                repo_base_path=os.environ.get("IAP_CODE_REPO_BASE", "")
            ),
            micro_resolver=MicroSymbolResolver(
                repo_base_path=os.environ.get("IAP_CODE_REPO_BASE", "")
            ),
        )
        ctx.outbox_worker = InvestigationOutboxWorker(
            repository=ctx.investigation_repo,
            event_queue=ctx.outbox_queue,
        )
        if engine is not None and getattr(ctx, "engine", None) is None:
            ctx.engine = engine

        # Seed the default dev profile through the proxy so it lands in
        # Postgres when the primary is up (idempotent upsert).
        try:
            from investigation_agent_platform.api.dependencies import _DEFAULT_PROFILE

            if await ctx.profile_repo.get_by_application_id("tenant-a", "example-app") is None:
                await ctx.profile_repo.save("tenant-a", _DEFAULT_PROFILE)
        except Exception as exc:
            logger.warning("Default profile seed failed", extra={"error": str(exc)})

        ctx._buffer_wal = wal
        ctx._buffer_proxies = proxies
        ctx._buffer_interval = interval
        ctx._buffered = True
        pending = wal.pending_count()
        held = wal.held_count()
        logger.info(
            "Buffered persistence ready",
            extra={"wal": path, "pending": pending, "held": held, "primary": reachable},
        )
        if held:
            logger.error(
                "HELD buffer entries need operator review",
                extra={"held": held, "wal": path},
            )
        return ctx
    except Exception as exc:
        logger.warning(
            "Buffered persistence wiring failed; continuing with plain context",
            extra={"error": str(exc)},
        )
        return ctx


def start_buffer_flusher(ctx: Any) -> asyncio.Task | None:
    """Start the background WAL flusher for a buffered context (lifespan)."""
    from investigation_agent_platform.infrastructure.persistence.buffered import flusher_loop

    if not getattr(ctx, "_buffered", False):
        return None
    if getattr(ctx, "_buffer_flusher_task", None) is not None:
        return ctx._buffer_flusher_task
    stop = asyncio.Event()
    ctx._buffer_flusher_stop = stop
    task = asyncio.create_task(
        flusher_loop(ctx._buffer_proxies, ctx._buffer_wal, ctx._buffer_interval, stop)
    )
    ctx._buffer_flusher_task = task
    return task


async def stop_buffer_flusher(ctx: Any) -> None:
    """Stop the background WAL flusher (lifespan shutdown)."""
    stop = getattr(ctx, "_buffer_flusher_stop", None)
    task = getattr(ctx, "_buffer_flusher_task", None)
    if stop is not None:
        stop.set()
    if task is not None:
        try:
            await task
        except asyncio.CancelledError:
            pass
    wal = getattr(ctx, "_buffer_wal", None)
    if wal is not None:
        try:
            wal.close()
        except Exception as exc:
            logger.warning("Buffer WAL close failed", extra={"error": str(exc)})
