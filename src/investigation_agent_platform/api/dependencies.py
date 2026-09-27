# src/investigation_agent_platform/api/dependencies.py
"""In-memory dependency wiring for the API layer.

Provides a lightweight, DB-free composition root using in-memory repository
implementations so the HTTP API remains runnable in development and CI until
the production SQLAlchemy session factory is wired into the app context.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.application.investigation.services import (
    CancelInvestigationService,
    CreateInvestigationService,
    GetInvestigationService,
    ResumeInvestigationService,
)
from investigation_agent_platform.domain.common.exceptions import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
)
from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.domain.hypothesis.models import Hypothesis
from investigation_agent_platform.domain.investigation.models import Investigation
from investigation_agent_platform.domain.profile.models import (
    ApplicationProfile,
    CodeProfile,
    CorrelationProfile,
    InvestigationProfile,
    ObservabilityProfile,
    StateProfile,
)
from investigation_agent_platform.domain.timeline.models import TimelineEvent
from investigation_agent_platform.ports.persistence.repositories import (
    ApplicationProfileRepository,
    CheckpointRepository,
    EvidenceRepository,
    HypothesisRepository,
    InvestigationRepository,
    TimelineRepository,
    TransitionEventRepository,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkflowStatusEvent:
    investigation_id: UUID
    tenant_id: str
    new_status: str
    execution_result: dict[str, Any] | None = None


class InvestigationOutboxWorker:
    """Asynchronous background worker synchronizing Temporal workflow events with domain state."""

    def __init__(
        self,
        repository: InvestigationRepository,
        event_queue: asyncio.Queue[WorkflowStatusEvent],
    ) -> None:
        self._repo = repository
        self._queue = event_queue

    async def start_listening(self) -> None:
        while True:
            try:
                event: WorkflowStatusEvent = await self._queue.get()
                investigation = await self._repo.get_by_id(event.tenant_id, event.investigation_id)
                if investigation:
                    from investigation_agent_platform.domain.investigation.models import (
                        InvestigationStatus,
                    )

                    try:
                        investigation.status = InvestigationStatus(event.new_status)
                    except ValueError:
                        pass
                    if (
                        event.execution_result
                        and hasattr(investigation, "metadata")
                        and isinstance(investigation.metadata, dict)
                    ):
                        investigation.metadata.update(event.execution_result)
                    await self._repo.save(
                        event.tenant_id, investigation, expected_version=investigation.version
                    )
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as err:
                logger.error("Outbox worker processing error", extra={"error": str(err)})


class ApiSettings(BaseModel):
    """Minimal API settings for local/dev composition root."""

    model_config = ConfigDict(extra="allow")

    environment: str = Field(default="development")
    # F-065: docs are opt-in. The app factory additionally forces docs off in
    # production unless IAP_ENABLE_DOCS=true is set explicitly.
    enable_docs: bool = Field(default=False)
    # F-064: CORS allow-list (comma-separated origins). Empty = no CORS.
    cors_allow_origins: str = Field(default="")
    # F-064: trusted hosts (comma-separated). Empty = no TrustedHost enforcement.
    trusted_hosts: str = Field(default="")


class Container:
    """Minimal DI container stub for local dev."""

    def __init__(self, settings: ApiSettings | None = None) -> None:
        self.settings = settings or ApiSettings()

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None


class _InMemoryCheckpointRepository(CheckpointRepository):
    async def save_checkpoint(
        self,
        tenant_id: str,
        investigation_id: UUID,
        step_number: int,
        state_snapshot: dict[str, Any],
    ) -> None:
        return None

    async def get_latest_checkpoint(
        self, tenant_id: str, investigation_id: UUID
    ) -> dict[str, Any] | None:
        return None


class _InMemoryTransitionRepository(TransitionEventRepository):
    async def record_transition(
        self, tenant_id: str, investigation_id: UUID, from_state: str, to_state: str, reason: str
    ) -> None:
        return None


class InMemoryArtifactRepository:
    """Process-local envelope store mirroring the RLS visibility carve-out.

    Reads/writes are tenant-scoped except `list_shared_for_fingerprint`,
    which returns only SHARED_CODE_ISSUE rows — mirroring the DB policy so
    dev/test behavior matches production. Callers MUST still verify session
    membership before cross-tenant use.
    """

    def __init__(self) -> None:
        from investigation_agent_platform.domain.knowledge.models import KnowledgeArtifact

        self._store: dict[UUID, KnowledgeArtifact] = {}

    async def save(self, tenant_id: str, artifact: Any) -> None:
        if artifact.tenant_id != tenant_id:
            from investigation_agent_platform.domain.common.exceptions import ConcurrencyError

            raise ConcurrencyError("Artifact tenant mismatch")
        self._store[artifact.id] = artifact

    async def get_by_id(self, tenant_id: str, artifact_id: UUID) -> Any | None:
        from investigation_agent_platform.domain.knowledge.models import ArtifactVisibility

        row = self._store.get(artifact_id)
        if row is None:
            return None
        if row.tenant_id == tenant_id:
            return row
        if row.visibility == ArtifactVisibility.SHARED_CODE_ISSUE:
            return row
        return None

    async def list_for_investigation(self, tenant_id: str, investigation_id: UUID) -> list[Any]:
        return [
            a
            for a in self._store.values()
            if a.investigation_id == investigation_id and a.tenant_id == tenant_id
        ]

    async def list_active_for_reuse(
        self, tenant_id: str, application_id: str, kinds: list[str] | None = None
    ) -> list[Any]:
        from investigation_agent_platform.domain.knowledge.models import ArtifactStatus

        return [
            a
            for a in self._store.values()
            if a.tenant_id == tenant_id
            and a.application_id == application_id
            and a.status == ArtifactStatus.ACTIVE
            and (kinds is None or a.kind in kinds)
        ]

    async def list_shared_for_fingerprint(
        self, tenant_id: str, code_issue_fingerprint: str
    ) -> list[Any]:
        from investigation_agent_platform.domain.knowledge.models import (
            ArtifactStatus,
            ArtifactVisibility,
        )

        _ = tenant_id  # binds the (no-op, in-memory) session scope; see docstring
        return [
            a
            for a in self._store.values()
            if a.code_issue_fingerprint == code_issue_fingerprint
            and a.visibility == ArtifactVisibility.SHARED_CODE_ISSUE
            and a.status == ArtifactStatus.ACTIVE
        ]


class InMemorySessionRepository:
    """Process-local session store. Strictly tenant-scoped, no carve-out."""

    def __init__(self) -> None:
        from investigation_agent_platform.domain.knowledge.models import InvestigationSession

        self._store: dict[UUID, InvestigationSession] = {}

    async def append(self, tenant_id: str, session: Any) -> None:
        if session.tenant_id != tenant_id:
            from investigation_agent_platform.domain.common.exceptions import ConcurrencyError

            raise ConcurrencyError("Session tenant mismatch")
        self._store[session.id] = session

    async def list_own_sessions(self, tenant_id: str, investigation_id: UUID) -> list[Any]:
        return sorted(
            (
                s
                for s in self._store.values()
                if s.investigation_id == investigation_id and s.tenant_id == tenant_id
            ),
            key=lambda s: s.session_number,
        )

    async def count_own_sessions(self, tenant_id: str, investigation_id: UUID) -> int:
        return len(await self.list_own_sessions(tenant_id, investigation_id))


class InMemoryCodeIssueIndex:
    """Process-local tenant-free coordination index (mirrors SQL advisory-lock numbering)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._rows: dict[tuple[str, int], tuple[UUID, Any]] = {}

    async def record_session(
        self,
        code_issue_fingerprint: str,
        session_number: int,
        investigation_id: UUID,
        occurred_at: Any,
    ) -> None:
        async with self._lock:
            self._rows.setdefault(
                (code_issue_fingerprint, session_number), (investigation_id, occurred_at)
            )

    async def sessions_for_fingerprint(
        self, code_issue_fingerprint: str
    ) -> list[tuple[int, UUID, Any]]:
        async with self._lock:
            rows = [
                (num, inv_id, ts)
                for (fp, num), (inv_id, ts) in self._rows.items()
                if fp == code_issue_fingerprint
            ]
        return sorted(rows, key=lambda r: r[0])

    async def latest_investigation(
        self, code_issue_fingerprint: str, open_only: bool = False
    ) -> UUID | None:
        _ = open_only  # status filtering is the caller's job
        rows = await self.sessions_for_fingerprint(code_issue_fingerprint)
        return rows[-1][1] if rows else None

    async def next_session_number(self, code_issue_fingerprint: str) -> int:
        async with self._lock:
            numbers = [num for (fp, num) in self._rows if fp == code_issue_fingerprint]
            return (max(numbers) if numbers else 0) + 1


class _InMemoryOutboxRepository:
    """Process-local outbox (durable Postgres version in production)."""

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []

    async def enqueue(
        self,
        tenant_id: str,
        investigation_id: UUID | None,
        event_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> None:
        if any(
            e["tenant_id"] == tenant_id and e["idempotency_key"] == idempotency_key
            for e in self._events
        ):
            return
        self._events.append(
            {
                "tenant_id": tenant_id,
                "investigation_id": investigation_id,
                "event_type": event_type,
                "idempotency_key": idempotency_key,
                "payload": payload,
                "dispatched": False,
            }
        )

    async def dispatch_pending(self, tenant_id: str, publisher: Any, batch_size: int = 25) -> int:
        dispatched = 0
        for event in self._events:
            if dispatched >= batch_size:
                break
            if event["tenant_id"] != tenant_id or event["dispatched"]:
                continue
            try:
                from investigation_agent_platform.ports.messaging.publisher import EventEnvelope

                envelope = EventEnvelope(
                    event_type=event["event_type"],
                    tenant_id=event["tenant_id"],
                    investigation_id=event["investigation_id"],
                    idempotency_key=event["idempotency_key"],
                    payload=event["payload"],
                )
                await publisher.publish(envelope)
                event["dispatched"] = True
                dispatched += 1
            except Exception:
                continue
        return dispatched


class _InMemoryActionExecutionRepository:
    """Process-local action-execution audit log (durable Postgres version in Phase 2 / F-068)."""

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []

    async def record_action(
        self,
        tenant_id: str,
        investigation_id: UUID,
        action: Any,
        result_status: str,
        principal_id: str = "worker",
        policy_version: str = "v1",
    ) -> None:
        self._records.append(
            {
                "tenant_id": tenant_id,
                "investigation_id": investigation_id,
                "action_id": getattr(action, "action_id", None),
                "action_type": getattr(getattr(action, "action_type", None), "value", None),
                "result_status": result_status,
                "principal_id": principal_id,
                "policy_version": policy_version,
            }
        )

    async def count_by_investigation(self, tenant_id: str, investigation_id: UUID) -> int:
        return sum(
            1
            for r in self._records
            if r["tenant_id"] == tenant_id and r["investigation_id"] == investigation_id
        )


class InMemoryInvestigationRepository(InvestigationRepository):
    def __init__(self) -> None:
        self._store: dict[tuple[str, UUID], Investigation] = {}

    async def create(self, tenant_id: str, investigation: Investigation) -> None:
        self._store[(tenant_id, investigation.id)] = investigation

    async def get_by_id(self, tenant_id: str, investigation_id: UUID) -> Investigation | None:
        inv = self._store.get((tenant_id, investigation_id))
        if inv is not None and inv.tenant_id != tenant_id:
            return None
        return inv

    async def save(
        self, tenant_id: str, investigation: Investigation, expected_version: int
    ) -> None:
        current = self._store.get((tenant_id, investigation.id))
        if current is None or current.version != expected_version:
            from investigation_agent_platform.domain.common.exceptions import ConcurrencyError

            raise ConcurrencyError("Investigation version mismatch")
        # Ensure stored tenant matches the partition key
        if investigation.tenant_id != tenant_id:
            from investigation_agent_platform.domain.common.exceptions import ConcurrencyError

            raise ConcurrencyError("Investigation tenant mismatch")
        self._store[(tenant_id, investigation.id)] = investigation

    async def delete(self, tenant_id: str, investigation_id: UUID) -> None:
        self._store.pop((tenant_id, investigation_id), None)

    async def exists(self, tenant_id: str, investigation_id: UUID) -> bool:
        return (tenant_id, investigation_id) in self._store


class InMemoryApplicationProfileRepository(ApplicationProfileRepository):
    def __init__(self) -> None:
        self._profiles: dict[tuple[str, str], ApplicationProfile] = {}

    async def get_by_application_id(
        self, tenant_id: str, application_id: str, version: str | None = None
    ) -> ApplicationProfile | None:
        # F-034: honor exact version resolution — a version mismatch is a miss,
        # not a silent fallback to whatever revision happens to be stored.
        profile = self._profiles.get((tenant_id, application_id))
        if profile is None:
            return None
        if version is not None and str(profile.version) != str(version):
            return None
        return profile

    async def list(self, tenant_id: str) -> list[ApplicationProfile]:
        return [p for (t, _), p in self._profiles.items() if t == tenant_id]

    async def save(self, tenant_id: str, profile: ApplicationProfile) -> None:
        self._profiles[(tenant_id, profile.id)] = profile


class InMemoryEvidenceRepository(EvidenceRepository):
    def __init__(self) -> None:
        self._store: dict[UUID, Evidence] = {}
        # F-036: tenant-scoped composite keys — tenant is a mandatory partition
        # key, never a post-filter over a globally keyed map.
        self._by_investigation: dict[tuple[str, UUID], list[UUID]] = {}
        self._by_fingerprint: dict[tuple[str, str], UUID] = {}

    def _is_duplicate_fingerprint(self, tenant_id: str, evidence: Evidence) -> bool:
        key = (tenant_id, evidence.fingerprint)
        existing = self._by_fingerprint.get(key)
        return existing is not None and existing != evidence.evidence_id

    async def save(
        self, tenant_id: str, evidence: Evidence, investigation_id: UUID | None = None
    ) -> None:
        if self._is_duplicate_fingerprint(tenant_id, evidence):
            return
        self._store[evidence.evidence_id] = evidence
        self._by_fingerprint[(tenant_id, evidence.fingerprint)] = evidence.evidence_id
        if investigation_id is not None:
            ids = self._by_investigation.setdefault((tenant_id, investigation_id), [])
            if evidence.evidence_id not in ids:
                ids.append(evidence.evidence_id)

    async def save_batch(
        self, tenant_id: str, evidence_list: list[Evidence], investigation_id: UUID | None = None
    ) -> None:
        seen: set[str] = set()
        for evidence in evidence_list:
            if evidence.fingerprint in seen:
                continue
            seen.add(evidence.fingerprint)
            await self.save(tenant_id, evidence, investigation_id)

    async def get_by_id(self, tenant_id: str, evidence_id: UUID) -> Evidence | None:
        ev = self._store.get(evidence_id)
        if ev is None or ev.tenant_id != tenant_id:
            return None
        return ev

    async def get_by_ids(self, tenant_id: str, evidence_ids: list[UUID]) -> list[Evidence]:
        result: list[Evidence] = []
        for eid in evidence_ids:
            ev = self._store.get(eid)
            if ev is not None and ev.tenant_id == tenant_id:
                result.append(ev)
        return result

    async def find_by_investigation_id(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[Evidence]:
        ids = self._by_investigation.get((tenant_id, investigation_id), [])
        return [
            self._store[i]
            for i in ids
            if i in self._store and self._store[i].tenant_id == tenant_id
        ]


class InMemoryTimelineRepository(TimelineRepository):
    def __init__(self) -> None:
        self._events: list[TimelineEvent] = []
        self._by_investigation: dict[tuple[str, UUID], list[TimelineEvent]] = {}

    async def append(
        self, tenant_id: str, event: TimelineEvent, investigation_id: UUID | None = None
    ) -> None:
        self._events.append(event)
        if investigation_id is not None:
            self._by_investigation.setdefault((tenant_id, investigation_id), []).append(event)

    async def append_batch(
        self, tenant_id: str, events: list[TimelineEvent], investigation_id: UUID | None = None
    ) -> None:
        for event in events:
            await self.append(tenant_id, event, investigation_id)

    async def find_by_investigation_id(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[TimelineEvent]:
        return [
            e
            for e in self._by_investigation.get((tenant_id, investigation_id), [])
            if e.tenant_id == tenant_id
        ]

    async def find_by_time_range(
        self, tenant_id: str, investigation_id: UUID, start: datetime, end: datetime
    ) -> list[TimelineEvent]:
        return [
            e
            for e in self._by_investigation.get((tenant_id, investigation_id), [])
            if e.tenant_id == tenant_id and start <= e.timestamp <= end
        ]

    async def find_by_investigation_and_tenant(
        self, investigation_id: UUID, tenant_id: str, offset: int = 0, limit: int = 100
    ) -> tuple[list[TimelineEvent], int]:
        filtered = [
            e
            for e in self._by_investigation.get((tenant_id, investigation_id), [])
            if e.tenant_id == tenant_id
        ]
        total = len(filtered)
        return filtered[offset : offset + limit], total


class InMemoryHypothesisRepository(HypothesisRepository):
    def __init__(self) -> None:
        self._store: dict[tuple[str, UUID], tuple[Hypothesis, UUID | None]] = {}

    async def save(
        self, tenant_id: str, hypothesis: Hypothesis, investigation_id: UUID | None = None
    ) -> None:
        self._store[(tenant_id, hypothesis.id)] = (hypothesis, investigation_id)

    async def get_by_id(self, tenant_id: str, hypothesis_id: UUID) -> Hypothesis | None:
        entry = self._store.get((tenant_id, hypothesis_id))
        if entry is None:
            return None
        h, _ = entry
        if h.tenant_id != tenant_id:
            return None
        return h

    async def find_by_investigation_id(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[Hypothesis]:
        return [
            h
            for (t, _), (h, iid) in self._store.items()
            if t == tenant_id and iid == investigation_id and h.tenant_id == tenant_id
        ]

    async def find_by_investigation_and_tenant(
        self, investigation_id: UUID, tenant_id: str, offset: int = 0, limit: int = 50
    ) -> tuple[list[Hypothesis], int]:
        filtered = [
            h
            for (t, _), (h, iid) in self._store.items()
            if t == tenant_id and iid == investigation_id and h.tenant_id == tenant_id
        ]
        total = len(filtered)
        return filtered[offset : offset + limit], total


# Extend timeline repo with paginated helper expected by routers
# async def _timeline_find_by_investigation_and_tenant(
#     self: InMemoryTimelineRepository,
#     investigation_id: UUID,
#     tenant_id: str,
#     offset: int = 0,
#     limit: int = 100,
# ) -> tuple[list[TimelineEvent], int]:
#     filtered = [
#         e
#         for e in self._by_investigation.get((tenant_id, investigation_id), [])
#         if e.tenant_id == tenant_id
#     ]
#     total = len(filtered)
#     return filtered[offset : offset + limit], total
#
#
# # Monkey-patch helper onto class for router compatibility
# InMemoryTimelineRepository.find_by_investigation_and_tenant = (  # type: ignore[attr-defined]
#     _timeline_find_by_investigation_and_tenant
# )


class AppContext:
    """Simple composition root for the API routers."""

    def __init__(
        self,
        investigation_repo: InvestigationRepository | None = None,
        profile_repo: ApplicationProfileRepository | None = None,
        evidence_repo: EvidenceRepository | None = None,
        timeline_repo: TimelineRepository | None = None,
        hypothesis_repo: HypothesisRepository | Any | None = None,
    ) -> None:
        self.investigation_repo = investigation_repo or InMemoryInvestigationRepository()
        self.profile_repo = profile_repo or InMemoryApplicationProfileRepository()
        self.evidence_repo = evidence_repo or InMemoryEvidenceRepository()
        self.timeline_repo = timeline_repo or InMemoryTimelineRepository()
        self.hypothesis_repo: Any = hypothesis_repo or InMemoryHypothesisRepository()
        # Minimal idempotency store for investigations router
        self.idempotency_store: Any = _InMemoryIdempotencyStore()
        # Shared checkpoint/transition repos so audit records persist for the
        # lifetime of the process instead of being discarded per-call.
        self.checkpoint_repo: Any = _InMemoryCheckpointRepository()
        self.transition_repo: Any = _InMemoryTransitionRepository()
        self.action_repo: Any = _InMemoryActionExecutionRepository()
        self.outbox_repo: Any = _InMemoryOutboxRepository()
        # Part 6 Slice 0: knowledge layer defaults (durable Postgres in production).
        self.artifact_repo: Any = InMemoryArtifactRepository()
        self.session_repo: Any = InMemorySessionRepository()
        self.code_issue_index: Any = InMemoryCodeIssueIndex()
        # Part 6 Slice 1: Mem0 projection adapter. None = envelopes only
        # (wired in bootstrap when mem0_enabled).
        self.knowledge_store: Any = None
        # Part 6 Slice 2: Graphiti temporal projection. None = envelopes
        # only (wired in bootstrap when graphiti_enabled).
        self.temporal_port: Any = None

        self.outbox_queue: asyncio.Queue[WorkflowStatusEvent] = asyncio.Queue()
        self.outbox_worker = InvestigationOutboxWorker(
            repository=self.investigation_repo,
            event_queue=self.outbox_queue,
        )

        # Mandatory action-authorization gate (F-004): grounded in the tenant's
        # ApplicationProfile rather than an unconditional allow.
        from investigation_agent_platform.infrastructure.security.profile_authorizer import (
            ProfileBasedActionAuthorizer,
            ProfileBasedCapabilityRegistry,
        )

        self.action_authorizer: Any = ProfileBasedActionAuthorizer(self.profile_repo)
        self.capability_registry: Any = ProfileBasedCapabilityRegistry(self.profile_repo)

        # Layer 3 topology (prompt1_v1.md): dev/test default is the in-memory
        # adapter behind the same ports the Neo4j adapter implements.
        # Production composition (bootstrap/__init__.py) overrides these with
        # the Neo4j-backed adapter and profile-backed repository registry
        # when IAP_TOPOLOGY_ENABLED=true.
        from investigation_agent_platform.application.topology.attribution import (
            FailureAttributionService,
        )
        from investigation_agent_platform.infrastructure.evidence.code.codeowners import (
            CodeownersResolver,
        )
        from investigation_agent_platform.infrastructure.evidence.code.micro import (
            MicroSymbolResolver,
        )
        from investigation_agent_platform.infrastructure.topology.in_memory import (
            InMemoryTopologyAdapter,
        )
        from investigation_agent_platform.infrastructure.topology.profile_repository_registry import (
            ProfileBackedRepositoryRegistry,
        )

        self.topology_adapter: Any = InMemoryTopologyAdapter()
        self.repository_registry: Any = ProfileBackedRepositoryRegistry(self.profile_repo)
        # ISSUE-6/ISSUE-3 graceful tiers, same convention as the code
        # intelligence provider: base path from IAP_CODE_REPO_BASE, empty
        # means the tiers are gracefully unavailable.
        _code_repo_base = os.environ.get("IAP_CODE_REPO_BASE", "")
        self.failure_attribution_service: Any = FailureAttributionService(
            attribution_port=self.topology_adapter,
            evidence_repo=self.evidence_repo,
            repository_registry=self.repository_registry,
            codeowners_resolver=CodeownersResolver(repo_base_path=_code_repo_base),
            micro_resolver=MicroSymbolResolver(repo_base_path=_code_repo_base),
        )
        self._seed_default_profile()

    def get_outbox_queue(self) -> asyncio.Queue[WorkflowStatusEvent]:
        return self.outbox_queue

    def _seed_default_profile(self) -> None:
        repo = self.profile_repo
        if not isinstance(repo, InMemoryApplicationProfileRepository):

            async def _maybe_seed() -> None:
                if await repo.get_by_application_id("tenant-a", "example-app") is None:
                    await repo.save("tenant-a", _DEFAULT_PROFILE)

            try:
                asyncio.get_running_loop()
                # If already in event loop, schedule as task
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as _pool:
                    _pool.submit(asyncio.run, _maybe_seed()).result()
            except RuntimeError:
                asyncio.run(_maybe_seed())
            return
        if repo._profiles.get(("tenant-a", "example-app")) is None:
            repo._profiles[("tenant-a", "example-app")] = _DEFAULT_PROFILE

    def create_investigation_service(self) -> CreateInvestigationService:
        return CreateInvestigationService(
            investigation_repo=self.investigation_repo,
            profile_repo=self.profile_repo,
        )

    def get_investigation_service(self) -> GetInvestigationService:
        return GetInvestigationService(investigation_repo=self.investigation_repo)

    def resume_investigation_service(self) -> ResumeInvestigationService:
        return ResumeInvestigationService(
            investigation_repo=self.investigation_repo,
            profile_repo=self.profile_repo,
            evidence_repo=self.evidence_repo,
            timeline_repo=self.timeline_repo,
            checkpoint_repo=self.checkpoint_repo,
        )

    def cancel_investigation_service(self) -> CancelInvestigationService:
        return CancelInvestigationService(
            investigation_repo=self.investigation_repo,
            transition_repo=self.transition_repo,
        )

    def execute_action_service(self) -> Any:
        from investigation_agent_platform.application.investigation.services import (
            ExecuteActionService,
        )

        return ExecuteActionService(
            action_repo=self.action_repo,
            evidence_repo=self.evidence_repo,
            authorizer=self.action_authorizer,
            capability_registry=self.capability_registry,
        )

    def error_intake_service(self, clock: Any = None) -> Any:
        from investigation_agent_platform.application.knowledge.intake import ErrorIntakeService

        return ErrorIntakeService(
            investigation_repo=self.investigation_repo,
            session_repo=self.session_repo,
            code_issue_index=self.code_issue_index,
            clock=clock,
        )

    def knowledge_capture_service(self, clock_now: Any = None) -> Any:
        from investigation_agent_platform.application.knowledge.capture import (
            KnowledgeCaptureService,
        )

        return KnowledgeCaptureService(
            artifact_repo=self.artifact_repo,
            evidence_repo=self.evidence_repo,
            investigation_repo=self.investigation_repo,
            clock_now=clock_now,
            knowledge_store=self.knowledge_store,
        )

    def knowledge_retrieval_service(
        self, checkers: Any = None, max_reverify_attempts: int = 3, clock_now: Any = None
    ) -> Any:
        from investigation_agent_platform.application.knowledge.retrieve import (
            KnowledgeRetrievalService,
        )

        return KnowledgeRetrievalService(
            artifact_repo=self.artifact_repo,
            checkers=checkers,
            max_reverify_attempts=max_reverify_attempts,
            clock_now=clock_now,
            knowledge_store=self.knowledge_store,
            temporal_port=getattr(self, "temporal_port", None),
        )


class _InMemoryIdempotencyStore:
    """Thread-safe in-memory idempotency store with TTL and atomic check-and-set.

    Uses ``asyncio.Lock`` to make ``get``/``set`` atomic and to allow
    callers to perform a check-then-execute-then-set sequence without
    TOCTOU races via ``async with store.lock`` combined with the
    ``*_under_lock`` helpers (which assume the caller already holds the
    lock and therefore do not re-acquire it).
    """

    def __init__(self, ttl_seconds: float = 86400) -> None:
        self._store: dict[str, tuple[Any, float]] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self._ttl_seconds: float = ttl_seconds

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    @property
    def ttl_seconds(self) -> float:
        return self._ttl_seconds

    def _is_expired(self, expires_at: float) -> bool:
        return time.monotonic() > expires_at

    def get_under_lock(self, key: str) -> Any | None:
        """Return value if present and not expired. Caller must hold ``lock``."""
        entry = self._store.get(key)
        if entry is None:
            return None
        value, exp = entry
        if self._is_expired(exp):
            self._store.pop(key, None)
            return None
        return value

    def set_under_lock(self, key: str, value: Any) -> None:
        """Store value with TTL. Caller must hold ``lock``."""
        self._store[key] = (value, time.monotonic() + self._ttl_seconds)

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            return self.get_under_lock(key)

    async def set(self, key: str, value: Any) -> None:
        async with self._lock:
            self.set_under_lock(key, value)

    async def get_or_create(self, key: str, value: Any) -> tuple[Any, bool]:
        """Atomic check-and-set.

        Returns ``(stored_value, created)`` where ``created`` is True if
        the key was absent/expired and has now been inserted.
        """
        async with self._lock:
            existing = self.get_under_lock(key)
            if existing is not None:
                return existing, False
            self.set_under_lock(key, value)
            return value, True

    async def reserve_or_get(
        self, tenant_id: str, key: str, request_hash: str
    ) -> tuple[dict[str, Any] | None, bool]:
        """Reserve ownership of ``key`` or return the previously completed response.

        Returns ``(cached_response, reserved)``. ``reserved=True`` means the
        caller now owns this key and must call ``complete`` after executing
        the operation. Raises ``IdempotencyConflictError`` if the key was
        already used with a different request payload (F-014), and
        ``IdempotencyInProgressError`` if a concurrent request for the same
        key is still executing.
        """
        composite = f"{tenant_id}:{key}"
        async with self._lock:
            entry = self._store.get(composite)
            if entry is None:
                self._store[composite] = (
                    {"request_hash": request_hash, "status": "PENDING", "response": None},
                    time.monotonic() + self._ttl_seconds,
                )
                return None, True
            value, exp = entry
            if self._is_expired(exp):
                self._store[composite] = (
                    {"request_hash": request_hash, "status": "PENDING", "response": None},
                    time.monotonic() + self._ttl_seconds,
                )
                return None, True
            if value.get("request_hash") != request_hash:
                raise IdempotencyConflictError(
                    f"Idempotency key '{key}' already used with a different request payload"
                )
            if value.get("status") == "PENDING":
                raise IdempotencyInProgressError(
                    f"A request with idempotency key '{key}' is already in progress"
                )
            return value.get("response"), False

    async def complete(self, tenant_id: str, key: str, response: dict[str, Any]) -> None:
        composite = f"{tenant_id}:{key}"
        async with self._lock:
            entry = self._store.get(composite)
            request_hash = entry[0].get("request_hash") if entry else ""
            self._store[composite] = (
                {"request_hash": request_hash, "status": "DONE", "response": response},
                time.monotonic() + self._ttl_seconds,
            )


_context: AppContext | None = None


_DEFAULT_PROFILE = ApplicationProfile(
    id="example-app",
    tenant_id="tenant-a",
    name="Example Payments Service",
    description="Default example application profile for local development",
    environment="production",
    observability=ObservabilityProfile(
        provider="elasticsearch",
        indices=["logs-app", "traces-app"],
        timestampField="@timestamp",
        serviceField="service.name",
        environmentField="deployment.environment",
        sessionField="session.id",
        requestField="request.id",
        traceField="trace.id",
        logLevelField="severity",
    ),
    state=StateProfile(
        provider="postgres",
        database="app_db",
        schema="public",
        tables=["orders", "payments", "ledger"],
        primaryIdentifiers=["transaction_id", "order_id"],
        stateFields=["status", "amount", "currency"],
        timestampFields=["created_at", "updated_at"],
        queryTemplates={"find_transaction": "SELECT * FROM {table} WHERE {pk} = :id"},
    ),
    code=CodeProfile(
        provider="git",
        repository="example/payments-service",
        defaultBranch="main",
        language="python",
        sourceRoots=["src"],
        buildSystem="uv",
        moduleStructure="src/payments",
    ),
    correlation=CorrelationProfile(fields=["session.id", "request.id", "trace.id"]),
    investigation=InvestigationProfile(
        defaultTimeWindow=3600,
        maximumEvidencePerQuery=50,
        maximumInvestigationDuration=7200,
        enabledEvidenceTypes=["LOG", "TRACE", "DATABASE_STATE", "COMMIT_HISTORY"],
        correlationDepth=2,
        maximumHypotheses=5,
    ),
)


def get_app_context() -> AppContext:
    global _context
    if _context is None:
        _context = AppContext()
    return _context


def set_app_context(context: AppContext | Any) -> None:
    global _context
    if isinstance(context, AppContext):
        _context = context
    else:
        # Allow Container stub to seed AppContext
        _context = AppContext()
