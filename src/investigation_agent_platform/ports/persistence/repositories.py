# src/investigation_agent_platform/ports/persistence/repositories.py
"""Tenant-aware persistence repository driven port protocols."""

from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from investigation_agent_platform.domain.common.background_job import (
    BackgroundJob,
    BackgroundJobStatus,
)
from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.domain.finding.clustering import (
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

    async def list_open_ids(self, tenant_id: str) -> list[UUID]:
        """IDs of investigations not yet in a terminal status (ISSUE-4:
        feeds the snapshot-retention janitor's pinned-revision lookup)."""
        ...


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
    """Repository protocol for ApplicationProfile reading, writing, and versioning.

    Part 11.2: profiles are immutable revisions. ``save()`` must never
    overwrite an already-written ``(tenant_id, id, version)`` — a new
    revision requires a higher ``version``. ``get_by_application_id`` with
    ``version=None`` resolves the latest revision.
    """

    async def get_by_application_id(
        self, tenant_id: str, application_id: str, version: str | None = None
    ) -> ApplicationProfile | None: ...

    async def list_revisions(self, tenant_id: str, application_id: str) -> list[ApplicationProfile]:
        """All historical revisions for one application id, newest first."""
        ...

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

    async def save_finding_record(self, tenant_id: str, finding: Finding) -> None:
        """Persist one typed finding (idempotent on finding id)."""
        ...

    async def save_conclusion(self, tenant_id: str, conclusion: InvestigationConclusion) -> None:
        """Persist the terminal conclusion (upsert per investigation)."""
        ...

    async def get_conclusion(
        self, tenant_id: str, investigation_id: UUID
    ) -> InvestigationConclusion | None: ...

    async def list_findings(
        self,
        tenant_id: str,
        investigation_id: UUID,
        finding_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Finding], int]:
        """Findings for one investigation with total count (paginated)."""
        ...

    async def get_findings_by_ids(
        self, tenant_id: str, finding_ids: list[UUID]
    ) -> list[Finding]: ...

    async def list_tenant_findings(
        self, tenant_id: str, limit: int = 100, offset: int = 0
    ) -> tuple[list[Finding], int]:
        """Tenant-wide findings, newest first (Part 11.7 clustering input)."""
        ...


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


@runtime_checkable
class BackgroundJobRepository(Protocol):
    """Tenant-scoped background job persistence with OCC (Part 11.3B)."""

    async def create(self, job: BackgroundJob) -> None: ...

    async def get_by_id(self, tenant_id: str, job_id: UUID) -> BackgroundJob | None: ...

    async def save(self, tenant_id: str, job: BackgroundJob, expected_version: int) -> None:
        """Upsert with optimistic concurrency; version mismatch raises ConcurrencyError."""
        ...

    async def list_jobs(
        self,
        tenant_id: str,
        kind: str | None = None,
        status: BackgroundJobStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[BackgroundJob], int]: ...


@runtime_checkable
class InputRequirementRepository(Protocol):
    """Tenant-scoped input requirement + fulfillment persistence (Part 11.5)."""

    async def create_requirement(self, tenant_id: str, requirement: InputRequirement) -> None: ...

    async def get_pending(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[InputRequirement]: ...

    async def get_by_id(self, tenant_id: str, requirement_id: UUID) -> InputRequirement | None: ...

    async def set_state(
        self,
        tenant_id: str,
        requirement_id: UUID,
        expected_version: int,
        state: RequirementState,
    ) -> None:
        """Compare-and-set requirement state; version mismatch raises ConcurrencyError."""
        ...

    async def record_fulfillment(self, tenant_id: str, fulfillment: InputFulfillment) -> None:
        """Insert immutable audit row; idempotent on (tenant, requirement, version)."""
        ...

    async def list_fulfillments(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[InputFulfillment]: ...


@runtime_checkable
class FindingClusterRepository(Protocol):
    """Tenant-scoped cluster taxonomy + assignment history (Part 11.7)."""

    async def current_revision(self, tenant_id: str) -> int: ...

    async def load_taxonomy(self, tenant_id: str) -> list[FindingCluster]: ...

    async def create_cluster(self, tenant_id: str, cluster: FindingCluster) -> None: ...

    async def assign_finding(self, tenant_id: str, assignment: FindingClusterAssignment) -> None:
        """Record membership; closes any prior open assignment for the finding."""
        ...

    async def assigned_finding_ids(self, tenant_id: str, finding_ids: list[UUID]) -> set[UUID]:
        """Subset of ids with an open (valid_to IS NULL) assignment."""
        ...

    async def assignments_for_finding(
        self, tenant_id: str, finding_id: UUID
    ) -> list[FindingClusterAssignment]: ...

    async def cluster_assignments(
        self,
        tenant_id: str,
        cluster_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[FindingClusterAssignment], int]: ...

    async def find_candidate_clusters(
        self, tenant_id: str, query_text: str, limit: int = 12
    ) -> list[FindingCluster]:
        """Lexical candidates over the current taxonomy (bounded)."""
        ...

    async def upsert_finding_embedding(
        self, tenant_id: str, embedding: FindingEmbedding
    ) -> None: ...

    async def active_generation(
        self, tenant_id: str, embedding_model: str, embedding_version: str
    ) -> int:
        """Newest ACTIVE generation = the read generation (blue/green)."""
        ...

    async def find_similar_assigned_clusters(
        self, tenant_id: str, query_text: str, limit: int = 12
    ) -> list[UUID]:
        """Cluster ids whose member findings lexically match (bounded)."""
        ...
