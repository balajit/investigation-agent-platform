"""In-memory dependency wiring for the API layer.

Provides a lightweight, DB-free composition root using in-memory repository
implementations so the HTTP API remains runnable in development and CI until
the production SQLAlchemy session factory is wired into the app context.
"""

from __future__ import annotations

import asyncio
import time
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


class ApiSettings(BaseModel):
    """Minimal API settings for local/dev composition root."""

    model_config = ConfigDict(extra="allow")

    environment: str = Field(default="development")
    enable_docs: bool = Field(default=True)


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
        self, tenant_id: str, investigation_id: UUID, step_number: int, state_snapshot: dict[str, Any]
    ) -> None:
        return None

    async def get_latest_checkpoint(self, tenant_id: str, investigation_id: UUID) -> dict[str, Any] | None:
        return None


class _InMemoryTransitionRepository(TransitionEventRepository):
    async def record_transition(
        self, tenant_id: str, investigation_id: UUID, from_state: str, to_state: str, reason: str
    ) -> None:
        return None


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

    async def save(self, tenant_id: str, investigation: Investigation, expected_version: int) -> None:
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
        return self._profiles.get((tenant_id, application_id))

    async def list(self, tenant_id: str) -> list[ApplicationProfile]:
        return [p for (t, _), p in self._profiles.items() if t == tenant_id]

    async def save(self, tenant_id: str, profile: ApplicationProfile) -> None:
        self._profiles[(tenant_id, profile.id)] = profile


class InMemoryEvidenceRepository(EvidenceRepository):
    def __init__(self) -> None:
        self._store: dict[UUID, Evidence] = {}
        self._by_investigation: dict[UUID, list[UUID]] = {}
        self._by_fingerprint: dict[tuple[str, str], UUID] = {}

    def _is_duplicate_fingerprint(self, tenant_id: str, evidence: Evidence) -> bool:
        key = (tenant_id, evidence.fingerprint)
        existing = self._by_fingerprint.get(key)
        return existing is not None and existing != evidence.evidence_id

    async def save(self, tenant_id: str, evidence: Evidence, investigation_id: UUID | None = None) -> None:
        if self._is_duplicate_fingerprint(tenant_id, evidence):
            return
        self._store[evidence.evidence_id] = evidence
        self._by_fingerprint[(tenant_id, evidence.fingerprint)] = evidence.evidence_id
        if investigation_id is not None:
            ids = self._by_investigation.setdefault(investigation_id, [])
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

    async def find_by_investigation_id(self, tenant_id: str, investigation_id: UUID) -> list[Evidence]:
        ids = self._by_investigation.get(investigation_id, [])
        return [self._store[i] for i in ids if i in self._store and self._store[i].tenant_id == tenant_id]


class InMemoryTimelineRepository(TimelineRepository):
    def __init__(self) -> None:
        self._events: list[TimelineEvent] = []
        self._by_investigation: dict[UUID, list[TimelineEvent]] = {}

    async def append(self, tenant_id: str, event: TimelineEvent, investigation_id: UUID | None = None) -> None:
        self._events.append(event)
        if investigation_id is not None:
            self._by_investigation.setdefault(investigation_id, []).append(event)

    async def append_batch(
        self, tenant_id: str, events: list[TimelineEvent], investigation_id: UUID | None = None
    ) -> None:
        for event in events:
            await self.append(tenant_id, event, investigation_id)

    async def find_by_investigation_id(self, tenant_id: str, investigation_id: UUID) -> list[TimelineEvent]:
        return [e for e in self._by_investigation.get(investigation_id, []) if e.tenant_id == tenant_id]

    async def find_by_time_range(
        self, tenant_id: str, investigation_id: UUID, start: datetime, end: datetime
    ) -> list[TimelineEvent]:
        return [
            e
            for e in self._by_investigation.get(investigation_id, [])
            if e.tenant_id == tenant_id and start <= e.timestamp <= end
        ]


class InMemoryHypothesisRepository(HypothesisRepository):
    def __init__(self) -> None:
        self._store: dict[UUID, tuple[Hypothesis, UUID | None]] = {}

    async def save(self, tenant_id: str, hypothesis: Hypothesis, investigation_id: UUID | None = None) -> None:
        self._store[hypothesis.id] = (hypothesis, investigation_id)

    async def get_by_id(self, tenant_id: str, hypothesis_id: UUID) -> Hypothesis | None:
        entry = self._store.get(hypothesis_id)
        if entry is None:
            return None
        h, _ = entry
        if h.tenant_id != tenant_id:
            return None
        return h

    async def find_by_investigation_id(self, tenant_id: str, investigation_id: UUID) -> list[Hypothesis]:
        return [h for h, iid in self._store.values() if iid == investigation_id and h.tenant_id == tenant_id]

    async def find_by_investigation_and_tenant(
        self, investigation_id: UUID, tenant_id: str, offset: int = 0, limit: int = 50
    ) -> tuple[list[Hypothesis], int]:
        filtered = [h for h, iid in self._store.values() if iid == investigation_id and h.tenant_id == tenant_id]
        total = len(filtered)
        return filtered[offset : offset + limit], total


# Extend timeline repo with paginated helper expected by routers
async def _timeline_find_by_investigation_and_tenant(
    self: InMemoryTimelineRepository, investigation_id: UUID, tenant_id: str, offset: int = 0, limit: int = 100
) -> tuple[list[TimelineEvent], int]:
    filtered = [e for e in self._by_investigation.get(investigation_id, []) if e.tenant_id == tenant_id]
    total = len(filtered)
    return filtered[offset : offset + limit], total


# Monkey-patch helper onto class for router compatibility
InMemoryTimelineRepository.find_by_investigation_and_tenant = _timeline_find_by_investigation_and_tenant  # type: ignore[attr-defined]


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
        self._seed_default_profile()

    def _seed_default_profile(self) -> None:
        repo = self.profile_repo
        if not isinstance(repo, InMemoryApplicationProfileRepository):

            async def _maybe_seed() -> None:
                if await repo.get_by_application_id("tenant-a", "example-app") is None:
                    await repo.save("tenant-a", _DEFAULT_PROFILE)

            import asyncio

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
            checkpoint_repo=_InMemoryCheckpointRepository(),
        )

    def cancel_investigation_service(self) -> CancelInvestigationService:
        return CancelInvestigationService(
            investigation_repo=self.investigation_repo,
            transition_repo=_InMemoryTransitionRepository(),
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
