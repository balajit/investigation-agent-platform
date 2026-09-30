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
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.application.investigation.services import (
    CancelInvestigationService,
    CreateInvestigationService,
    GetInvestigationService,
    ResumeInvestigationService,
)
from investigation_agent_platform.domain.common.background_job import (
    BackgroundJob,
    BackgroundJobStatus,
)
from investigation_agent_platform.domain.common.exceptions import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
)
from investigation_agent_platform.domain.common.extension import CapabilityScope
from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.domain.finding.clustering import (
    UNASSIGNED_CLUSTER_ID,
    FindingCluster,
    FindingClusterAssignment,
    FindingEmbedding,
)
from investigation_agent_platform.domain.finding.models import (
    Finding,
    InvestigationConclusion,
)
from investigation_agent_platform.domain.hypothesis.models import Hypothesis
from investigation_agent_platform.domain.investigation.input_requirements import (
    InputFulfillment,
    InputRequirement,
    RequirementState,
)
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
from investigation_agent_platform.ports.artifacts.store import (
    ArtifactObject,
    ArtifactPage,
    ArtifactRef,
    ArtifactStorePort,
)
from investigation_agent_platform.ports.persistence.repositories import (
    ApplicationProfileRepository,
    BackgroundJobRepository,
    CheckpointRepository,
    EvidenceRepository,
    FindingClusterRepository,
    FindingConclusionRepository,
    HypothesisRepository,
    InputRequirementRepository,
    InvestigationRepository,
    TimelineRepository,
    TransitionEventRepository,
)

if TYPE_CHECKING:
    # Infrastructure/adapter types for explicit AppContext ownership (Part 11.0).
    # TYPE_CHECKING-only to avoid import cycles: dependencies.py is imported by
    # bootstrap, which owns the concrete wiring.
    from faststream.kafka import KafkaBroker
    from sqlalchemy.ext.asyncio import AsyncEngine
    from temporalio.client import Client as TemporalClient

    from investigation_agent_platform.application.evidence.gateway import AsyncEvidenceGateway
    from investigation_agent_platform.application.extensions.registries import (
        CredentialProviderRegistry,
    )
    from investigation_agent_platform.application.investigation.composition import (
        InvestigationServices,
    )
    from investigation_agent_platform.infrastructure.configuration.config import (
        TemporalConfig,
        TopologyConfig,
    )
    from investigation_agent_platform.infrastructure.messaging.faststream import (
        KafkaEventPublisher,
    )
    from investigation_agent_platform.infrastructure.persistence.buffered import SqliteWal
    from investigation_agent_platform.ports.knowledge.ports import (
        KnowledgeStorePort,
        TemporalKnowledgePort,
    )
    from investigation_agent_platform.ports.observability.telemetry import ObservabilityPort
    from investigation_agent_platform.ports.security.quotas import QuotaEnforcerPort

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
    """Explicit DI container for API lifespan wiring (Part 11.0).

    Holds exactly one ``AppContext`` — either injected (production bootstrap
    preserves its already-built context by passing it here), built from an
    ``ApplicationConfig`` via the shared ``build_app_context`` composition
    root, or a fresh in-memory context for local dev/test. ``initialize()``
    is idempotent. The lifespan must call ``set_app_context`` with
    ``container.context`` (an ``AppContext``), never with the container itself.
    """

    def __init__(
        self,
        settings: ApiSettings | None = None,
        context: AppContext | None = None,
        app_config: Any | None = None,
    ) -> None:
        self.settings = settings or ApiSettings()
        self._context = context
        self._app_config = app_config

    @property
    def context(self) -> AppContext:
        """The composed context; built explicitly by ``initialize()``."""
        if self._context is None:
            self._context = AppContext()
        return self._context

    async def initialize(self) -> None:
        if self._context is not None:
            return
        if self._app_config is not None:
            from investigation_agent_platform.bootstrap import build_app_context

            self._context = build_app_context(self._app_config)
            return
        self._context = AppContext()

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

    async def list_expired_ttl(self, tenant_id: str, cutoff: Any, limit: int = 500) -> list[Any]:
        from investigation_agent_platform.domain.knowledge.models import (
            ArtifactStatus,
            RefreshPolicy,
        )

        rows = [
            a
            for a in self._store.values()
            if a.tenant_id == tenant_id
            and a.status == ArtifactStatus.ACTIVE
            and a.refresh_policy == RefreshPolicy.TTL
            and a.valid_to is not None
            and a.valid_to <= cutoff
        ]
        rows.sort(key=lambda a: a.valid_to)
        return rows[:limit]


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

    async def list_open_ids(self, tenant_id: str) -> list[UUID]:
        from investigation_agent_platform.domain.investigation.models import InvestigationStatus

        terminal = {
            InvestigationStatus.COMPLETED,
            InvestigationStatus.FAILED,
            InvestigationStatus.CANCELLED,
        }
        return [
            inv_id
            for (t, inv_id), inv in self._store.items()
            if t == tenant_id and inv.status not in terminal
        ]


class InMemoryApplicationProfileRepository(ApplicationProfileRepository):
    """Process-local profile store (Part 11.2: immutable revisions).

    ``_profiles`` holds the latest-revision pointer per ``(tenant_id, id)`` —
    kept for backward compatibility with existing direct-poke test fixtures
    that set it without going through ``save()``. ``_revisions`` is the real
    per-version history that ``save()`` populates; exact-version lookups
    check it first, falling back to the pointer only when no history entry
    exists (covers the direct-poke case).
    """

    def __init__(self) -> None:
        self._profiles: dict[tuple[str, str], ApplicationProfile] = {}
        self._revisions: dict[tuple[str, str], dict[int, ApplicationProfile]] = {}

    async def get_by_application_id(
        self, tenant_id: str, application_id: str, version: str | None = None
    ) -> ApplicationProfile | None:
        key = (tenant_id, application_id)
        if version is None:
            return self._profiles.get(key)
        try:
            wanted = int(version)
        except (TypeError, ValueError):
            return None
        revision = self._revisions.get(key, {}).get(wanted)
        if revision is not None:
            return revision
        # F-034: exact version resolution — a version mismatch is a miss,
        # not a silent fallback to whatever revision happens to be stored.
        pointer = self._profiles.get(key)
        if pointer is not None and pointer.version == wanted:
            return pointer
        return None

    async def list_revisions(self, tenant_id: str, application_id: str) -> list[ApplicationProfile]:
        key = (tenant_id, application_id)
        revisions = dict(self._revisions.get(key, {}))
        pointer = self._profiles.get(key)
        if pointer is not None:
            revisions.setdefault(pointer.version, pointer)
        return [revisions[v] for v in sorted(revisions, reverse=True)]

    async def list(self, tenant_id: str) -> list[ApplicationProfile]:
        return [p for (t, _), p in self._profiles.items() if t == tenant_id]

    async def save(self, tenant_id: str, profile: ApplicationProfile) -> None:
        key = (tenant_id, profile.id)
        # Immutable: never overwrite an already-written revision. A same-version
        # resubmission is a no-op (matches SQL on-conflict-do-nothing); only a
        # strictly higher version advances the latest pointer.
        self._revisions.setdefault(key, {}).setdefault(profile.version, profile)
        current = self._profiles.get(key)
        if current is None or profile.version > current.version:
            self._profiles[key] = profile


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


class InMemoryFindingConclusionRepository(FindingConclusionRepository):
    """Process-local findings/conclusions store (Part 11.2).

    Tenant-composite keys throughout (F-036 precedent); conclusion is an
    upsert per (tenant, investigation), mirroring the SQL unique constraint.
    """

    def __init__(self) -> None:
        self._findings: dict[tuple[str, UUID], Finding] = {}
        self._conclusions: dict[tuple[str, UUID], InvestigationConclusion] = {}

    async def save_finding(
        self, tenant_id: str, investigation_id: UUID, finding_type: str, details: dict[str, Any]
    ) -> None:
        try:
            finding = Finding.model_validate({**details, "tenant_id": tenant_id})
            await self.save_finding_record(tenant_id, finding)
            return
        except Exception:
            pass
        if any(
            f.tenant_id == tenant_id
            and f.investigation_id == investigation_id
            and f.finding_type.value == finding_type
            and f.title == str(details.get("title", finding_type))
            for f in self._findings.values()
        ):
            return
        from investigation_agent_platform.domain.finding.models import FindingType

        self._findings[(tenant_id, uuid4())] = Finding(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            finding_type=FindingType(finding_type),
            title=str(details.get("title", finding_type))[:256],
            statement=str(details.get("statement", "") or finding_type)[:2048],
        )

    async def save_finding_record(self, tenant_id: str, finding: Finding) -> None:
        if finding.tenant_id != tenant_id:
            from investigation_agent_platform.domain.common.exceptions import ConcurrencyError

            raise ConcurrencyError("Finding tenant mismatch")
        self._findings.setdefault((tenant_id, finding.id), finding)

    async def save_conclusion(self, tenant_id: str, conclusion: InvestigationConclusion) -> None:
        if conclusion.tenant_id != tenant_id:
            from investigation_agent_platform.domain.common.exceptions import ConcurrencyError

            raise ConcurrencyError("Conclusion tenant mismatch")
        self._conclusions[(tenant_id, conclusion.investigation_id)] = conclusion

    async def get_conclusion(
        self, tenant_id: str, investigation_id: UUID
    ) -> InvestigationConclusion | None:
        conclusion = self._conclusions.get((tenant_id, investigation_id))
        if conclusion is not None and conclusion.tenant_id != tenant_id:
            return None
        return conclusion

    async def list_findings(
        self,
        tenant_id: str,
        investigation_id: UUID,
        finding_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Finding], int]:
        filtered = [
            f
            for (t, _), f in self._findings.items()
            if t == tenant_id
            and f.tenant_id == tenant_id
            and f.investigation_id == investigation_id
            and (finding_type is None or f.finding_type.value == finding_type)
        ]
        total = len(filtered)
        return filtered[offset : offset + limit], total

    async def get_findings_by_ids(self, tenant_id: str, finding_ids: list[UUID]) -> list[Finding]:
        wanted = set(finding_ids)
        return [
            f
            for (t, _), f in self._findings.items()
            if t == tenant_id and f.tenant_id == tenant_id and f.id in wanted
        ]

    async def list_tenant_findings(
        self, tenant_id: str, limit: int = 100, offset: int = 0
    ) -> tuple[list[Finding], int]:
        """Tenant-wide findings, newest first (Part 11.7 clustering input)."""
        filtered = sorted(
            (
                f
                for (t, _), f in self._findings.items()
                if t == tenant_id and f.tenant_id == tenant_id
            ),
            key=lambda f: f.created_at,
            reverse=True,
        )
        return filtered[offset : offset + limit], len(filtered)


class InMemoryInputRequirementRepository(InputRequirementRepository):
    """Process-local requirement/fulfillment store (Part 11.5)."""

    def __init__(self) -> None:
        self._requirements: dict[tuple[str, UUID], InputRequirement] = {}
        self._fulfillments: dict[tuple[str, UUID], list[InputFulfillment]] = {}

    async def create_requirement(self, tenant_id: str, requirement: InputRequirement) -> None:
        if requirement.tenant_id != tenant_id:
            from investigation_agent_platform.domain.common.exceptions import ConcurrencyError

            raise ConcurrencyError("Input requirement tenant mismatch")
        self._requirements[(tenant_id, requirement.requirement_id)] = requirement

    async def get_pending(self, tenant_id: str, investigation_id: UUID) -> list[InputRequirement]:
        return sorted(
            (
                req
                for (t, _), req in self._requirements.items()
                if t == tenant_id
                and req.tenant_id == tenant_id
                and req.investigation_id == investigation_id
                and req.state == RequirementState.PENDING
            ),
            key=lambda req: req.requested_at,
        )

    async def get_by_id(self, tenant_id: str, requirement_id: UUID) -> InputRequirement | None:
        req = self._requirements.get((tenant_id, requirement_id))
        if req is not None and req.tenant_id != tenant_id:
            return None
        return req

    async def set_state(
        self,
        tenant_id: str,
        requirement_id: UUID,
        expected_version: int,
        state: RequirementState,
    ) -> None:
        from investigation_agent_platform.domain.common.exceptions import ConcurrencyError

        req = self._requirements.get((tenant_id, requirement_id))
        if req is None or req.tenant_id != tenant_id:
            raise ConcurrencyError(
                f"Input requirement {requirement_id} not found",
                details={"requirement_id": str(requirement_id)},
            )
        if req.requirement_version != expected_version:
            raise ConcurrencyError(
                f"Input requirement {requirement_id} version mismatch",
                details={
                    "requirement_id": str(requirement_id),
                    "expected_version": expected_version,
                },
            )
        self._requirements[(tenant_id, requirement_id)] = req.model_copy(update={"state": state})

    async def record_fulfillment(self, tenant_id: str, fulfillment: InputFulfillment) -> None:
        if fulfillment.tenant_id != tenant_id:
            from investigation_agent_platform.domain.common.exceptions import ConcurrencyError

            raise ConcurrencyError("Input fulfillment tenant mismatch")
        key = (tenant_id, fulfillment.requirement_id)
        existing = self._fulfillments.setdefault(key, [])
        for row in existing:
            if (
                row.requirement_version == fulfillment.requirement_version
                and row.content_digest == fulfillment.content_digest
            ):
                return
        existing.append(fulfillment)

    async def list_fulfillments(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[InputFulfillment]:
        rows = [
            row
            for rows in self._fulfillments.values()
            for row in rows
            if row.tenant_id == tenant_id and row.investigation_id == investigation_id
        ]
        return sorted(rows, key=lambda row: row.fulfilled_at)


class InMemoryBackgroundJobRepository(BackgroundJobRepository):
    """Process-local background job store with OCC (Part 11.3B)."""

    def __init__(self) -> None:
        self._jobs: dict[tuple[str, UUID], BackgroundJob] = {}

    async def create(self, job: BackgroundJob) -> None:
        self._jobs[(job.tenant_id, job.id)] = job

    async def get_by_id(self, tenant_id: str, job_id: UUID) -> BackgroundJob | None:
        job = self._jobs.get((tenant_id, job_id))
        if job is not None and job.tenant_id != tenant_id:
            return None
        return job

    async def save(self, tenant_id: str, job: BackgroundJob, expected_version: int) -> None:
        from investigation_agent_platform.domain.common.exceptions import ConcurrencyError

        if job.tenant_id != tenant_id:
            raise ConcurrencyError("Background job tenant mismatch")
        current = self._jobs.get((tenant_id, job.id))
        if current is None or current.version != expected_version:
            raise ConcurrencyError("Background job version mismatch")
        self._jobs[(tenant_id, job.id)] = job

    async def list_jobs(
        self,
        tenant_id: str,
        kind: str | None = None,
        status: BackgroundJobStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[BackgroundJob], int]:
        filtered = [
            job
            for (t, _), job in self._jobs.items()
            if t == tenant_id
            and job.tenant_id == tenant_id
            and (kind is None or job.kind == kind)
            and (status is None or job.status == status)
        ]
        filtered.sort(key=lambda job: job.created_at, reverse=True)
        total = len(filtered)
        return filtered[offset : offset + limit], total


class InMemoryFindingClusterRepository(FindingClusterRepository):
    """Process-local cluster taxonomy + assignment history (Part 11.7)."""

    def __init__(self) -> None:
        self._clusters: dict[tuple[str, UUID], FindingCluster] = {}
        self._assignments: dict[tuple[str, UUID], FindingClusterAssignment] = {}
        self._embeddings: dict[tuple[str, UUID, str, str, int], FindingEmbedding] = {}

    async def current_revision(self, tenant_id: str) -> int:
        revisions = [
            cluster.taxonomy_revision
            for (tenant, _), cluster in self._clusters.items()
            if tenant == tenant_id
        ]
        return max(revisions, default=0)

    async def load_taxonomy(self, tenant_id: str) -> list[FindingCluster]:
        revision = await self.current_revision(tenant_id)
        if revision == 0:
            return []
        return sorted(
            (
                cluster
                for (tenant, _), cluster in self._clusters.items()
                if tenant == tenant_id and cluster.taxonomy_revision == revision
            ),
            key=lambda cluster: cluster.cluster_key,
        )

    async def create_cluster(self, tenant_id: str, cluster: FindingCluster) -> None:
        if cluster.tenant_id != tenant_id:
            from investigation_agent_platform.domain.common.exceptions import ConcurrencyError

            raise ConcurrencyError("Cluster tenant mismatch")
        if cluster.cluster_key == UNASSIGNED_CLUSTER_ID:
            from investigation_agent_platform.domain.common.exceptions import ConcurrencyError

            raise ConcurrencyError("UNASSIGNED is a bucket, not a taxonomy row")
        key = (tenant_id, cluster.id)
        if key not in self._clusters:
            self._clusters[key] = cluster

    async def assign_finding(self, tenant_id: str, assignment: FindingClusterAssignment) -> None:
        if assignment.tenant_id != tenant_id:
            from investigation_agent_platform.domain.common.exceptions import ConcurrencyError

            raise ConcurrencyError("Assignment tenant mismatch")
        now = assignment.valid_from
        for key, row in list(self._assignments.items()):
            if (
                key[0] == tenant_id
                and row.finding_id == assignment.finding_id
                and row.valid_to is None
            ):
                self._assignments[key] = row.model_copy(update={"valid_to": now})
        self._assignments[(tenant_id, assignment.id)] = assignment

    async def assigned_finding_ids(self, tenant_id: str, finding_ids: list[UUID]) -> set[UUID]:
        wanted = set(finding_ids)
        return {
            row.finding_id
            for (tenant, _), row in self._assignments.items()
            if tenant == tenant_id and row.finding_id in wanted and row.valid_to is None
        }

    async def assignments_for_finding(
        self, tenant_id: str, finding_id: UUID
    ) -> list[FindingClusterAssignment]:
        return sorted(
            (
                row
                for (tenant, _), row in self._assignments.items()
                if tenant == tenant_id and row.finding_id == finding_id
            ),
            key=lambda row: row.valid_from,
            reverse=True,
        )

    async def cluster_assignments(
        self,
        tenant_id: str,
        cluster_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[FindingClusterAssignment], int]:
        filtered = sorted(
            (
                row
                for (tenant, _), row in self._assignments.items()
                if tenant == tenant_id and row.cluster_id == cluster_id and row.valid_to is None
            ),
            key=lambda row: row.valid_from,
            reverse=True,
        )
        return filtered[offset : offset + limit], len(filtered)

    async def find_candidate_clusters(
        self, tenant_id: str, query_text: str, limit: int = 12
    ) -> list[FindingCluster]:
        words = {word for word in query_text.lower().split() if len(word) >= 3}
        if not words:
            return []
        taxonomy = await self.load_taxonomy(tenant_id)
        scored = sorted(
            (
                (sum(1 for word in words if word in f"{c.label} {c.description}".lower()), c)
                for c in taxonomy
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )
        return [cluster for score, cluster in scored[: max(1, limit)] if score > 0]

    async def upsert_finding_embedding(self, tenant_id: str, embedding: FindingEmbedding) -> None:
        if embedding.tenant_id != tenant_id:
            from investigation_agent_platform.domain.common.exceptions import ConcurrencyError

            raise ConcurrencyError("Embedding tenant mismatch")
        key = (
            tenant_id,
            embedding.finding_id,
            embedding.embedding_model,
            embedding.embedding_version,
            embedding.generation,
        )
        self._embeddings[key] = embedding

    async def active_generation(
        self, tenant_id: str, embedding_model: str, embedding_version: str
    ) -> int:
        generations = [
            generation
            for (tenant, _, model, version, generation), row in self._embeddings.items()
            if tenant == tenant_id
            and model == embedding_model
            and version == embedding_version
            and row.lifecycle == "ACTIVE"
        ]
        return max(generations, default=0)

    async def find_similar_assigned_clusters(
        self, tenant_id: str, query_text: str, limit: int = 12
    ) -> list[UUID]:
        words = {word for word in query_text.lower().split() if len(word) >= 3}
        if not words:
            return []
        scored: dict[UUID, int] = {}
        for (tenant, finding_id, _, _, _), row in self._embeddings.items():
            if tenant != tenant_id or row.lifecycle != "ACTIVE":
                continue
            score = sum(1 for word in words if word in row.lexical_text.lower())
            if score == 0:
                continue
            for (_, _), assignment in self._assignments.items():
                if (
                    assignment.tenant_id == tenant_id
                    and assignment.finding_id == finding_id
                    and assignment.valid_to is None
                    and assignment.cluster_id is not None
                ):
                    scored[assignment.cluster_id] = max(scored.get(assignment.cluster_id, 0), score)
        ranked = sorted(scored.items(), key=lambda pair: pair[1], reverse=True)
        return [cluster_id for cluster_id, _ in ranked[: max(1, limit)]]


class InMemoryArtifactStore(ArtifactStorePort):
    """Process-local artifact store for dev/test (Part 11.3A)."""

    def __init__(self) -> None:
        self._objects: dict[str, ArtifactObject] = {}

    @staticmethod
    def _scope_key(scope_tenant: str, scope_app: str | None, key: str) -> str:
        from investigation_agent_platform.infrastructure.artifacts.keys import (
            build_object_key,
        )

        return build_object_key(scope_tenant, scope_app, key)

    async def put(
        self,
        scope: CapabilityScope,
        key: str,
        content: bytes,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> ArtifactRef:
        import hashlib

        from investigation_agent_platform.infrastructure.artifacts.keys import (
            MAX_METADATA_ENTRIES,
        )

        meta = dict(metadata or {})
        if len(meta) > MAX_METADATA_ENTRIES:
            raise ValueError("too many artifact metadata entries")
        store_key = self._scope_key(scope.tenant_id, scope.application_id, key)
        ref = ArtifactRef(
            artifact_id=uuid4(),
            key=key,
            content_digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
            content_type=content_type,
            size_bytes=len(content),
        )
        self._objects[store_key] = ArtifactObject(ref=ref, content=content, metadata=meta)
        return ref

    async def get(self, scope: CapabilityScope, ref: ArtifactRef) -> ArtifactObject:
        store_key = self._scope_key(scope.tenant_id, scope.application_id, ref.key)
        try:
            return self._objects[store_key]
        except KeyError:
            raise FileNotFoundError(f"artifact not found: {ref.key!r}") from None

    async def list(
        self,
        scope: CapabilityScope,
        prefix: str,
        cursor: str | None = None,
        limit: int = 50,
    ) -> ArtifactPage:
        _ = cursor
        scope_prefix = f"{scope.tenant_id}/{scope.application_id or '-'}/"
        items = [
            obj.ref
            for store_key, obj in sorted(self._objects.items())
            if store_key.startswith(scope_prefix + prefix)
        ]
        page = items[: max(1, min(limit, 200))]
        return ArtifactPage(items=page, next_cursor=None, has_more=len(items) > len(page))

    async def delete(self, scope: CapabilityScope, ref: ArtifactRef) -> None:
        store_key = self._scope_key(scope.tenant_id, scope.application_id, ref.key)
        self._objects.pop(store_key, None)

    async def signed_url(
        self, scope: CapabilityScope, ref: ArtifactRef, ttl_seconds: int = 3600
    ) -> str:
        _ = ttl_seconds
        store_key = self._scope_key(scope.tenant_id, scope.application_id, ref.key)
        if store_key not in self._objects:
            raise FileNotFoundError(f"artifact not found: {ref.key!r}")
        return f"memory://{store_key}"


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
        # Part 11.2: findings/conclusions — durable Postgres in production,
        # process-local store in dev/test (mirrors every other Part 6+ repo).
        self.finding_repo: FindingConclusionRepository = InMemoryFindingConclusionRepository()
        # Part 11.7: cluster taxonomy + assignments — durable Postgres in
        # production, process-local store in dev/test.
        self.finding_cluster_repo: FindingClusterRepository = InMemoryFindingClusterRepository()
        # Part 11.3: background jobs + artifacts + quota — durable Postgres /
        # object storage in production, process-local in dev/test.
        from investigation_agent_platform.application.extensions.registries import (
            CredentialProviderRegistry,
        )
        from investigation_agent_platform.infrastructure.messaging.job_fanout import (
            JobProgressHub,
        )
        from investigation_agent_platform.infrastructure.persistence.quota_enforcer import (
            InMemoryQuotaEnforcer,
        )

        self.background_job_repo: BackgroundJobRepository = InMemoryBackgroundJobRepository()
        self.input_repo: InputRequirementRepository = InMemoryInputRequirementRepository()
        self.artifact_store: ArtifactStorePort = InMemoryArtifactStore()
        self.quota_enforcer: QuotaEnforcerPort = InMemoryQuotaEnforcer()
        self.job_hub: JobProgressHub = JobProgressHub()
        # Part 11.4: outbound credential providers. Empty by default
        # (fail-closed: unknown provider ids raise); bootstrap and profile
        # wiring register OAuth2/static adapters with explicit config.
        self.credential_registry: CredentialProviderRegistry = CredentialProviderRegistry()
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
        self.knowledge_store: KnowledgeStorePort | None = None
        # Part 6 Slice 2: Graphiti temporal projection. None = envelopes
        # only (wired in bootstrap when graphiti_enabled).
        self.temporal_port: TemporalKnowledgePort | None = None

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
        # Part 11.0: explicitly owned infrastructure attachments. Production
        # bootstrap (or the dev buffered-dev wiring) assigns these; they
        # default to None so `getattr(ctx, ...)` probes and health checks keep
        # working on bare in-memory contexts. Declared here so no new
        # undeclared `Any` attachment can be added silently — mypy strict
        # rejects attribute writes that are not declared on this class.
        self.engine: AsyncEngine | None = None
        self.observability: ObservabilityPort | None = None
        self.broker: KafkaBroker | None = None
        self.event_publisher: KafkaEventPublisher | None = None
        self.temporal_client: TemporalClient | None = None
        self.temporal_config: TemporalConfig | None = None
        self.evidence_gateway: AsyncEvidenceGateway | None = None
        self.investigation_services: InvestigationServices | None = None
        self.topology_config: TopologyConfig | None = None
        # Buffered-dev persistence attachments (bootstrap/buffered_dev.py).
        self._buffer_wal: SqliteWal | None = None
        self._buffer_proxies: dict[str, Any] = {}
        self._buffer_interval: float = 5.0
        self._buffered: bool = False
        self._buffer_flusher_stop: Any = None
        self._buffer_flusher_task: Any = None
        # Shutdown idempotency guard for shutdown_app_context().
        self._shutdown_done: bool = False
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

    def knowledge_capture_service(
        self,
        clock_now: Any = None,
        evidence_summary_ttl_days: int = 90,
        max_episodes_per_investigation: int = 50,
    ) -> Any:
        from investigation_agent_platform.application.knowledge.capture import (
            KnowledgeCaptureService,
        )

        return KnowledgeCaptureService(
            artifact_repo=self.artifact_repo,
            evidence_repo=self.evidence_repo,
            investigation_repo=self.investigation_repo,
            clock_now=clock_now,
            knowledge_store=self.knowledge_store,
            temporal_port=getattr(self, "temporal_port", None),
            evidence_summary_ttl_days=evidence_summary_ttl_days,
            max_episodes_per_investigation=max_episodes_per_investigation,
            observability=getattr(self, "observability", None),
        )

    def knowledge_retrieval_service(
        self,
        checkers: Any = None,
        max_reverify_attempts: int = 3,
        clock_now: Any = None,
        attribution_port: Any = None,
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
            attribution_port=attribution_port
            if attribution_port is not None
            else getattr(self, "attribution_port", None),
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
        self,
        tenant_id: str,
        key: str,
        request_hash: str,
        operation: str = "default",
        application_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Reserve ownership of ``key`` or return the previously completed response.

        Returns ``(cached_response, reserved)``. ``reserved=True`` means the
        caller now owns this key and must call ``complete`` after executing
        the operation. Raises ``IdempotencyConflictError`` if the key was
        already used with a different request payload (F-014), and
        ``IdempotencyInProgressError`` if a concurrent request for the same
        key is still executing.

        Part 11.3D: ``(operation, application_id)`` scopes keys so batch,
        job, chat, and report operations never collide. Defaults preserve
        byte-identical keys for existing callers.
        """
        from investigation_agent_platform.domain.common.idempotency import (
            scoped_idempotency_key,
        )

        composite = f"{tenant_id}:{scoped_idempotency_key(key, operation, application_id)}"
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

    async def complete(
        self,
        tenant_id: str,
        key: str,
        response: dict[str, Any],
        operation: str = "default",
        application_id: str | None = None,
    ) -> None:
        from investigation_agent_platform.domain.common.idempotency import (
            scoped_idempotency_key,
        )

        composite = f"{tenant_id}:{scoped_idempotency_key(key, operation, application_id)}"
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


def set_app_context(context: AppContext) -> None:
    """Install the process-global application context (Part 11.0).

    Fail-closed: only ``AppContext`` instances are accepted. The previous
    behavior of silently coercing anything else (e.g. a ``Container`` stub)
    into a fresh in-memory context could wipe a bootstrapped production
    context at lifespan startup — that silent replacement is exactly what
    this rejects. Callers holding a ``Container`` must pass
    ``container.context`` after ``await container.initialize()``.
    """
    global _context
    if not isinstance(context, AppContext):
        raise TypeError(
            f"set_app_context requires an AppContext, got {type(context).__name__}; "
            "pass container.context after initialize(), never the container itself"
        )
    _context = context


async def shutdown_app_context(ctx: AppContext) -> None:
    """Ordered, idempotent shutdown for an ``AppContext`` (Part 11.0).

    Canonical order: background tasks first (buffer flusher), then message
    subscribers/plugin clients, then the event broker, then the database
    engine. Temporal clients need no explicit close in the pinned SDK. The
    outbox-worker task is owned by the API lifespan (``api/app.py``), which
    cancels it before calling this helper — so the full process shutdown
    order is: outbox worker → flusher → broker → engine.
    """
    if ctx._shutdown_done:
        return
    ctx._shutdown_done = True
    try:
        from investigation_agent_platform.bootstrap.buffered_dev import stop_buffer_flusher

        await stop_buffer_flusher(ctx)
    except Exception as exc:
        logger.warning("Error stopping buffer flusher: %s", exc)
    broker = ctx.broker
    if broker is not None and hasattr(broker, "close"):
        try:
            maybe = broker.close()
            if asyncio.iscoroutine(maybe):
                await maybe
        except Exception as exc:
            logger.warning("Error closing broker: %s", exc)
    engine = ctx.engine
    if engine is not None:
        try:
            await engine.dispose()
            logger.info("Disposed database engine on shutdown")
        except Exception as exc:
            logger.warning("Error disposing database engine: %s", exc)


def log_composition_summary(ctx: AppContext) -> None:
    """Log selected implementations + capability availability (Part 11.0).

    Names implementation classes and present/absent flags only — never URIs,
    credentials, tokens, or endpoint addresses.
    """
    logger.info(
        "Application composition summary",
        extra={
            "event": "composition",
            "investigation_repo": type(ctx.investigation_repo).__name__,
            "profile_repo": type(ctx.profile_repo).__name__,
            "evidence_repo": type(ctx.evidence_repo).__name__,
            "timeline_repo": type(ctx.timeline_repo).__name__,
            "hypothesis_repo": type(ctx.hypothesis_repo).__name__,
            "temporal": "present" if ctx.temporal_client is not None else "absent",
            "broker": "present" if ctx.broker is not None else "absent",
            "evidence_gateway": "present" if ctx.evidence_gateway is not None else "absent",
            "knowledge_store": "present" if ctx.knowledge_store is not None else "absent",
            "temporal_port": "present" if ctx.temporal_port is not None else "absent",
            "buffered": bool(ctx._buffered),
        },
    )
