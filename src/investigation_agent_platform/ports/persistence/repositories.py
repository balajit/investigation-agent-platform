# src/investigation_agent_platform/ports/persistence/repositories.py
"""Tenant-aware persistence repository driven port protocols."""

from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.domain.hypothesis.models import Hypothesis
from investigation_agent_platform.domain.investigation.models import (
    Investigation,
    InvestigationAction,
)
from investigation_agent_platform.domain.profile.models import ApplicationProfile
from investigation_agent_platform.domain.timeline.models import TimelineEvent


@runtime_checkable
class InvestigationRepository(Protocol):
    """Repository protocol for Investigation aggregate persistence with OCC and tenant boundaries."""

    async def create(self, tenant_id: str, investigation: Investigation) -> None: ...

    async def get_by_id(self, tenant_id: str, investigation_id: UUID) -> Investigation | None: ...

    async def save(
        self, tenant_id: str, investigation: Investigation, expected_version: int
    ) -> None: ...

    async def delete(self, tenant_id: str, investigation_id: UUID) -> None: ...

    async def exists(self, tenant_id: str, investigation_id: UUID) -> bool: ...


@runtime_checkable
class EvidenceRepository(Protocol):
    """Repository protocol for Evidence items with tenant scope."""

    async def save(
        self, tenant_id: str, evidence: Evidence, investigation_id: UUID | None = None
    ) -> None: ...

    async def save_batch(
        self, tenant_id: str, evidence_list: list[Evidence], investigation_id: UUID | None = None
    ) -> None: ...

    async def get_by_id(self, tenant_id: str, evidence_id: UUID) -> Evidence | None: ...

    async def get_by_ids(self, tenant_id: str, evidence_ids: list[UUID]) -> list[Evidence]: ...

    async def find_by_investigation_id(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[Evidence]: ...


@runtime_checkable
class HypothesisRepository(Protocol):
    """Repository protocol for Hypotheses persistence."""

    async def save(
        self, tenant_id: str, hypothesis: Hypothesis, investigation_id: UUID | None = None
    ) -> None: ...

    async def get_by_id(self, tenant_id: str, hypothesis_id: UUID) -> Hypothesis | None: ...

    async def find_by_investigation_id(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[Hypothesis]: ...


@runtime_checkable
class TimelineRepository(Protocol):
    """Repository protocol for TimelineEvent sequence persistence."""

    async def append(
        self, tenant_id: str, event: TimelineEvent, investigation_id: UUID | None = None
    ) -> None: ...

    async def append_batch(
        self, tenant_id: str, events: list[TimelineEvent], investigation_id: UUID | None = None
    ) -> None: ...

    async def find_by_investigation_id(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[TimelineEvent]: ...

    async def find_by_time_range(
        self, tenant_id: str, investigation_id: UUID, start: datetime, end: datetime
    ) -> list[TimelineEvent]: ...


@runtime_checkable
class ApplicationProfileRepository(Protocol):
    """Repository protocol for ApplicationProfile reading, writing, and versioning."""

    async def get_by_application_id(
        self, tenant_id: str, application_id: str, version: str | None = None
    ) -> ApplicationProfile | None: ...

    async def list(self, tenant_id: str) -> list[ApplicationProfile]: ...

    async def save(self, tenant_id: str, profile: ApplicationProfile) -> None: ...


@runtime_checkable
class CheckpointRepository(Protocol):
    """Repository protocol for persistent state checkpoints."""

    async def save_checkpoint(
        self,
        tenant_id: str,
        investigation_id: UUID,
        step_number: int,
        state_snapshot: dict[str, Any],
    ) -> None: ...

    async def get_latest_checkpoint(
        self, tenant_id: str, investigation_id: UUID
    ) -> dict[str, Any] | None: ...


@runtime_checkable
class TransitionEventRepository(Protocol):
    """Repository for recording investigation state transition events."""

    async def record_transition(
        self, tenant_id: str, investigation_id: UUID, from_state: str, to_state: str, reason: str
    ) -> None: ...


@runtime_checkable
class FindingConclusionRepository(Protocol):
    """Repository for storing final findings and conclusions."""

    async def save_finding(
        self, tenant_id: str, investigation_id: UUID, finding_type: str, details: dict[str, Any]
    ) -> None: ...


@runtime_checkable
class ActionExecutionRepository(Protocol):
    """Repository for auditing executed actions and tool calls."""

    async def record_action(
        self,
        tenant_id: str,
        investigation_id: UUID,
        action: InvestigationAction,
        result_status: str,
        principal_id: str = "worker",
        policy_version: str = "v1",
    ) -> None: ...


@runtime_checkable
class EvidenceRelationshipRepository(Protocol):
    """Repository for persisting evidence correlation relationships."""

    async def link_evidence(
        self, tenant_id: str, source_id: UUID, target_id: UUID, relationship_type: str
    ) -> None: ...

    async def fetch_relationships_for_evidence(
        self,
        tenant_id: str,
        application_id: str,
        root_evidence_ids: list[str],
        max_depth: int,
    ) -> list[dict[str, Any]]: ...
