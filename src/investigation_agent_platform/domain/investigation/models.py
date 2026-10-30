# src/investigation_agent_platform/domain/investigation/models.py
"""Investigation aggregate root, actions, limits, and runtime state models."""

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from investigation_agent_platform.domain.common.exceptions import (
    InvalidLifecycleTransitionException,
)
from investigation_agent_platform.domain.common.utils import (
    Clock,
    SystemClock,
    generate_execution_hash,
)
from investigation_agent_platform.domain.entity.models import InvestigationEntity
from investigation_agent_platform.domain.evidence.models import (
    Evidence,
    EvidenceReference,
    EvidenceRelationship,
    EvidenceType,
)
from investigation_agent_platform.domain.finding.models import Finding, InvestigationConclusion
from investigation_agent_platform.domain.hypothesis.models import Hypothesis
from investigation_agent_platform.domain.timeline.models import TimelineEvent


class InvestigationStatus(StrEnum):
    CREATED = "CREATED"
    CONTEXTUALIZING = "CONTEXTUALIZING"
    INVESTIGATING = "INVESTIGATING"
    CORRELATING = "CORRELATING"
    HYPOTHESIZING = "HYPOTHESIZING"
    VERIFYING = "VERIFYING"
    CONCLUDING = "CONCLUDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"
    # Part 11.5: workflow is suspended waiting for caller-supplied input
    # (see domain/investigation/input_requirements.py). Terminal-until-resumed.
    AWAITING_INPUT = "AWAITING_INPUT"
    # Dispatched but no workflow activity observed yet: set by POST .../start
    # (and .../retry) when the Temporal execution is accepted. Transient by
    # design — the worker's first transition moves out of it. First-class
    # member (not a DTO literal) so UI clients get a typed status (D14).
    RUNNING = "RUNNING"


class FactType(StrEnum):
    OBSERVATION = "OBSERVATION"
    STATE = "STATE"
    EVENT = "EVENT"
    ERROR = "ERROR"
    CODE_BEHAVIOR = "CODE_BEHAVIOR"
    CONFIGURATION = "CONFIGURATION"
    RELATIONSHIP = "RELATIONSHIP"


class ActionType(StrEnum):
    SEARCH_LOGS = "SEARCH_LOGS"
    QUERY_STATE = "QUERY_STATE"
    GET_CODE = "GET_CODE"
    SEARCH_CODE = "SEARCH_CODE"
    FIND_SYMBOL = "FIND_SYMBOL"
    FIND_CALL_GRAPH = "FIND_CALL_GRAPH"
    GET_CODE_HISTORY = "GET_CODE_HISTORY"
    PROBE_API = "PROBE_API"
    CORRELATE = "CORRELATE"
    # D2: stack walk (exception frames → ordered frame evidence) and logic
    # trace (attribute bundle → consuming-code decision path). Gathering
    # actions: participate in budgets, history, and forcing rules.
    WALK_STACK = "WALK_STACK"
    TRACE_LOGIC = "TRACE_LOGIC"
    # G-1: journey spine (session → ordered tracking-ID steps) and step
    # detail. Gathering actions like the rest.
    TRACE_JOURNEY = "TRACE_JOURNEY"
    # G-3: session-anchored entity-graph walk (anchor → relationship hops).
    WALK_SESSION_GRAPH = "WALK_SESSION_GRAPH"
    # T1-1: reference-docs search banking DOCUMENTATION rows (path+revision
    # provenance) for doc-citation verification.
    SEARCH_REF_DOCS = "SEARCH_REF_DOCS"
    FORMULATE_HYPOTHESIS = "FORMULATE_HYPOTHESIS"
    VERIFY_HYPOTHESIS = "VERIFY_HYPOTHESIS"
    CONCLUDE = "CONCLUDE"


class ActionOutcomeStatus(StrEnum):
    """Execution result of one dispatched action (P0-2).

    SUCCEEDED/EMPTY distinguish "evidence banked" from "ran clean, nothing
    found". DEGRADED covers provider-absent/path-miss degrade-to-zero paths
    that intentionally do not fail the investigation. FAILED/BLOCKED are set
    by the dispatcher on exception/authorization denial.
    """

    SUCCEEDED = "SUCCEEDED"
    EMPTY = "EMPTY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class ActionOutcome(BaseModel):
    """Typed post-execution result for one action (P0-2).

    Plain JSON scalars only — workflow history entries embed this verbatim
    and Temporal replay requires determinism-safe values.
    """

    model_config = ConfigDict(frozen=True)

    action_id: UUID = Field(default_factory=uuid4)
    action_type: ActionType
    status: ActionOutcomeStatus = ActionOutcomeStatus.SUCCEEDED
    evidence_ids: list[str] = Field(default_factory=list)
    item_count: int = Field(default=0, ge=0)
    error_code: str | None = Field(default=None, max_length=128)
    miss_reason: str | None = Field(default=None, max_length=256)
    duration_ms: int = Field(default=0, ge=0)
    # P3-2: per-source breakdown for fanned-out actions (QUERY_STATE across
    # datasources). Each entry: {source, status, items}. Empty for single.
    sources: list[dict[str, Any]] = Field(default_factory=list)
    # Fuzzy code-recall steering hint (verified-authority symbols only, never
    # cited): the gateway banks it in result provider_metadata, and the
    # handler copies the latest one here so the workflow can carry it into
    # the next turn's history. Direction only, like hub orientation.
    fuzzy_orientation: dict[str, Any] | None = Field(default=None)
    # WP-A3: retrieval attempt for this action (search query + distinct
    # outcome + novelty/lift/bound flags). JSON scalars only
    # (RetrievalAttempt dump); None = legacy row or non-retrieval
    # action. Diagnostics-only — never branched on.
    retrieval_attempt: dict[str, Any] | None = Field(default=None)


class ActorType(StrEnum):
    SYSTEM = "SYSTEM"
    ORCHESTRATOR = "ORCHESTRATOR"
    AGENT = "AGENT"
    USER = "USER"
    ADMIN = "ADMIN"


class InvestigationPriority(StrEnum):
    """Closed priority vocabulary for investigation requests (D12).

    Free strings previously flowed straight into persistence with no
    consumer ever branching on them; the UI had to guess with chips.
    """

    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class IncidentSeverity(StrEnum):
    """Closed severity vocabulary for error-intake reports (D12)."""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class CorrelationVector(BaseModel):
    model_config = ConfigDict(frozen=True)

    vector_id: UUID = Field(default_factory=uuid4)
    source_entity_id: UUID
    target_entity_id: UUID
    relationship_type: str = Field(..., max_length=128)
    weight: float = Field(default=1.0, ge=0.0, le=1.0)


class InvestigationObjective(BaseModel):
    model_config = ConfigDict(frozen=True)

    objective_id: UUID = Field(default_factory=uuid4)
    title: str = Field(..., max_length=256)
    description: str = Field(..., max_length=2048)
    target_system: str = Field(..., max_length=256)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EvidenceManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_id: UUID = Field(default_factory=uuid4)
    evidence_type: EvidenceType
    source: str = Field(..., description="URN or URI of source provider", max_length=512)
    payload_reference: str = Field(..., description="Tier 2 blob storage path", max_length=1024)
    summary: str = Field(..., description="Sanitized structural summary", max_length=2048)
    sanitized: bool = Field(default=True)
    relevance: float = Field(default=1.0, ge=0.0, le=1.0)
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class InvestigationAction(BaseModel):
    """Typed action intent proposed by LLM reasoner."""

    model_config = ConfigDict(frozen=True)

    action_id: UUID = Field(default_factory=uuid4)
    action_type: ActionType
    parameters: dict[str, Any] = Field(default_factory=dict)
    execution_hash: str = Field(..., max_length=128)

    @classmethod
    def create(
        cls,
        tenant_id: str,
        investigation_id: UUID,
        action_type: ActionType,
        parameters: dict[str, Any],
        capability: str = "default",
        provider: str = "default",
    ) -> "InvestigationAction":
        """Factory deriving deterministic fingerprint server-side.

        F-057: ``action_id`` is derived deterministically (UUIDv5 over the
        execution hash) so Temporal activity retries of the same logical
        action resolve to the same idempotency key instead of recording
        duplicate executions.
        """
        from uuid import NAMESPACE_URL, uuid5

        exec_hash = generate_execution_hash(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            action_type=action_type.value,
            parameters=parameters,
            capability=capability,
            provider=provider,
        )
        return cls(
            action_id=uuid5(NAMESPACE_URL, f"{tenant_id}:{investigation_id}:{exec_hash}"),
            action_type=action_type,
            parameters=parameters,
            execution_hash=exec_hash,
        )


class Fact(BaseModel):
    """Verified domain observation derived from evidence.

    Append-only semantics (TO_DO-024): ``Fact`` instances are immutable
    (``frozen=True``) and persistence only ever INSERTs new fact rows —
    ``SqlAlchemyInvestigationRepository.save`` appends one
    ``InvestigationFactORM`` row per fact and never UPDATEs or DELETEs
    facts. There is no read-modify-write on facts, so concurrent appends
    cannot lost-update each other; each concurrent writer's rows all
    survive. No ``version``/CAS field is needed because facts are never
    mutated after creation — only superseded by appending a newer fact.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    statement: str = Field(..., max_length=2048)
    fact_type: FactType
    source_evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("statement")
    @classmethod
    def _statement_must_be_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Fact statement must be non-blank")
        return value

    @field_validator("source_evidence_ids")
    @classmethod
    def _evidence_ids_must_be_unique(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("Fact source_evidence_ids must not contain duplicates")
        return value


class InvestigationRequest(BaseModel):
    """User request triggering an investigation execution."""

    model_config = ConfigDict(frozen=True)

    problem_description: str = Field(..., max_length=4096)
    application_id: str = Field(..., max_length=128)
    session_id: str = Field(..., max_length=128)
    requested_by: str = Field(..., max_length=128)
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    priority: InvestigationPriority = Field(default=InvestigationPriority.NORMAL)
    parameters: dict[str, Any] = Field(default_factory=dict)


class InvestigationContext(BaseModel):
    """Dynamic operational parameters discovered during analysis."""

    model_config = ConfigDict(frozen=True)

    environment: str = Field(..., max_length=64)
    region: str = Field(default="default", max_length=64)
    deployment_version: str = Field(default="unknown", max_length=128)
    service: str = Field(default="", max_length=128)
    host: str = Field(default="", max_length=128)
    time_window: tuple[datetime, datetime]
    known_identifiers: dict[str, str] = Field(default_factory=dict)
    user_context: dict[str, Any] = Field(default_factory=dict)
    application_metadata: dict[str, Any] = Field(default_factory=dict)


class InvestigationTransition(BaseModel):
    """Audit log entry for investigation lifecycle transition."""

    model_config = ConfigDict(frozen=True)

    from_status: InvestigationStatus
    to_status: InvestigationStatus
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    actor: ActorType
    reason: str = Field(..., max_length=1024)


class InvestigationLimits(BaseModel):
    """Hard upper bounds enforcing platform execution budget.

    This is the single enforced per-investigation budget: every issue is
    assigned these defaults at creation and the profile's evidence_budget
    (plus maximumHypotheses) overlays them at execution. Aligned with
    BudgetConfig/IAP_MAX_TOOL_CALLS defaults (50 tool calls) so the
    operator knob and the enforced value agree.
    """

    model_config = ConfigDict(frozen=True)

    max_duration_seconds: int = Field(default=1800, ge=10)
    max_tool_calls: int = Field(default=50, ge=1)
    max_iterations: int = Field(default=10, ge=1, le=100)
    max_llm_tokens: int = Field(default=1000000, ge=1000)
    max_evidence_items: int = Field(default=25, ge=1)
    max_hypotheses: int = Field(default=10, ge=1)
    max_correlation_depth: int = Field(default=3, ge=1)
    max_source_files: int = Field(default=20, ge=1)
    max_database_rows: int = Field(default=100, ge=1)
    max_log_query_window_minutes: int = Field(default=60, ge=1)


ANCHORS_VERSION = "anchors-v1"

#: Request-parameter keys carrying caller-supplied anchor bounds (ISO-8601).
ONSET_T_PARAM = "onset_t"
LAST_GOOD_G_PARAM = "last_good_g"


class InvestigationAnchors(BaseModel):
    """S1 intake anchors (DES-intake-anchors v1.0), version-pinned.

    Every investigation records onset T and last-known-good G, or explicit
    unknown (None + given=False — never a silent default: the absent bound
    is marked assumed downstream instead of being invented). spread_budget
    prices widening fetches against the per-investigation tool-call
    envelope. Recorded on the investigation metadata at creation; semantics
    changes ride anchors_version, never environment switches.
    """

    model_config = ConfigDict(frozen=True)

    anchors_version: str = Field(default=ANCHORS_VERSION, max_length=32)
    onset_t: datetime | None = None
    last_good_g: datetime | None = None
    onset_given: bool = False
    last_good_given: bool = False
    spread_budget: int = Field(default=50, ge=0)


def _parse_anchor_bound(value: Any, name: str) -> datetime | None:
    """Parse an optional ISO-8601 bound; None stays explicit-unknown.

    Fail-closed on explicit garbage: a present-but-unparseable value
    raises instead of silently degrading to unknown.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        # Normalize naive datetimes to UTC: window derivation compares
        # bounds against each other and against (aware) now, and mixing
        # naive with aware raises TypeError. ISO strings take the same
        # path below, so both input shapes behave identically.
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"anchor bound {name!r} is not ISO-8601: {text[:64]!r}")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    raise ValueError(f"anchor bound {name!r} must be ISO-8601 text, got {type(value).__name__}")


def stamp_anchors(
    parameters: dict[str, Any] | None,
    now: datetime,
    spread_budget: int,
) -> InvestigationAnchors:
    """Build S1 anchors from caller-supplied parameters (or explicit unknown).

    Reads ONSET_T_PARAM/LAST_GOOD_G_PARAM; absent keys mean explicit
    unknown, never assumed-now. The record shows which bounds were given
    via onset_given/last_good_given.
    """
    params = parameters or {}
    onset_raw = params.get(ONSET_T_PARAM)
    g_raw = params.get(LAST_GOOD_G_PARAM)
    onset = _parse_anchor_bound(onset_raw, ONSET_T_PARAM)
    last_good = _parse_anchor_bound(g_raw, LAST_GOOD_G_PARAM)
    _ = now
    return InvestigationAnchors(
        onset_t=onset,
        last_good_g=last_good,
        onset_given=onset_raw is not None
        and (not isinstance(onset_raw, str) or bool(onset_raw.strip())),
        last_good_given=g_raw is not None and (not isinstance(g_raw, str) or bool(g_raw.strip())),
        spread_budget=spread_budget,
    )


def anchors_from_metadata(metadata: dict[str, Any] | None) -> InvestigationAnchors:
    """Read anchors back; unmarked (pre-P1) rows resolve to explicit-unknown
    with the default envelope budget — current semantics for old rows."""
    raw = (metadata or {}).get("anchors")
    if not isinstance(raw, dict):
        return InvestigationAnchors()
    try:
        return InvestigationAnchors.model_validate(raw)
    except Exception:
        return InvestigationAnchors()


def derive_working_window(
    anchors: InvestigationAnchors,
    now: datetime,
    default_lookback_s: int,
) -> tuple[datetime, datetime, tuple[str, ...]]:
    """Working window [G, T] per DES-intake-anchors §2.

    Returns (start, end, assumed) where assumed names bounds that were
    not given ("onset", "last_good"). Unknown T anchors the end at now;
    unknown G anchors the start at end − lookback.
    """
    assumed: list[str] = []
    end = anchors.onset_t
    if end is None:
        end = now
        assumed.append("onset")
    elif not anchors.onset_given:
        assumed.append("onset")
    start = anchors.last_good_g
    if start is None:
        start = end - timedelta(seconds=default_lookback_s)
        assumed.append("last_good")
    elif not anchors.last_good_given:
        assumed.append("last_good")
    if start > end:
        start = end
    return start, end, tuple(assumed)


class InvestigationResult(BaseModel):
    """Execution response returned by infrastructure tool adapter."""

    model_config = ConfigDict(frozen=True)

    action_id: UUID
    status: str = Field(..., max_length=64)
    payload_content_uri: str = Field(..., max_length=1024)
    summary: str = Field(..., max_length=2048)
    execution_duration_ms: float = Field(..., ge=0.0)
    retrieved_evidence: list[Evidence] = Field(default_factory=list, max_length=50)


class InvestigationActionRecord(BaseModel):
    """Audit record pairing requested action with execution result."""

    model_config = ConfigDict(frozen=True)

    action: InvestigationAction
    result: InvestigationResult | None = None
    executed_at: datetime | None = None


class Investigation(BaseModel):
    """Aggregate root governing investigation state, history, and lifecycle transitions."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    session_id: str = Field(..., max_length=128)
    application_id: str = Field(..., max_length=128)
    tenant_id: str = Field(..., max_length=128)
    request: InvestigationRequest
    context: InvestigationContext
    status: InvestigationStatus = InvestigationStatus.CREATED
    # UI-facing workflow phase (GAP-05). Mirrors status today; kept as a
    # separate field so future workflow phases can diverge from lifecycle
    # status without a contract break. Maintained by transition_to.
    phase: str = Field(default="CREATED", max_length=64)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    facts: list[Fact] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    entities: list[InvestigationEntity] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    evidence_references: list[EvidenceReference] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    conclusion: InvestigationConclusion | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    version: int = Field(default=1, ge=1)
    code_issue_fingerprint: str | None = Field(
        default=None,
        max_length=128,
        description="Tenant-agnostic code-issue key linking merged sessions (Part 6 D8)",
    )

    def transition_to(
        self,
        new_status: InvestigationStatus,
        actor: ActorType,
        reason: str,
        clock: Clock = SystemClock(),  # noqa: B008 - stateless clock default, safe to share
    ) -> tuple["Investigation", InvestigationTransition]:
        """Validate and construct new immutable Investigation instance using supplied Clock."""
        valid_transitions: dict[InvestigationStatus, set[InvestigationStatus]] = {
            InvestigationStatus.CREATED: {
                InvestigationStatus.CONTEXTUALIZING,
                InvestigationStatus.INVESTIGATING,
                InvestigationStatus.PAUSED,
                InvestigationStatus.CANCELLED,
                InvestigationStatus.FAILED,
                InvestigationStatus.RUNNING,
            },
            InvestigationStatus.CONTEXTUALIZING: {
                InvestigationStatus.INVESTIGATING,
                InvestigationStatus.CORRELATING,
                InvestigationStatus.HYPOTHESIZING,
                InvestigationStatus.CONCLUDING,
                InvestigationStatus.PAUSED,
                InvestigationStatus.CANCELLED,
                InvestigationStatus.FAILED,
                InvestigationStatus.AWAITING_INPUT,
            },
            InvestigationStatus.INVESTIGATING: {
                InvestigationStatus.CORRELATING,
                InvestigationStatus.HYPOTHESIZING,
                InvestigationStatus.CONCLUDING,
                InvestigationStatus.PAUSED,
                InvestigationStatus.CANCELLED,
                InvestigationStatus.FAILED,
                InvestigationStatus.AWAITING_INPUT,
            },
            InvestigationStatus.CORRELATING: {
                InvestigationStatus.HYPOTHESIZING,
                InvestigationStatus.INVESTIGATING,
                InvestigationStatus.CONCLUDING,
                InvestigationStatus.PAUSED,
                InvestigationStatus.CANCELLED,
                InvestigationStatus.FAILED,
                InvestigationStatus.AWAITING_INPUT,
            },
            InvestigationStatus.HYPOTHESIZING: {
                InvestigationStatus.VERIFYING,
                InvestigationStatus.INVESTIGATING,
                InvestigationStatus.CONCLUDING,
                InvestigationStatus.PAUSED,
                InvestigationStatus.CANCELLED,
                InvestigationStatus.FAILED,
                InvestigationStatus.AWAITING_INPUT,
            },
            InvestigationStatus.VERIFYING: {
                InvestigationStatus.CONCLUDING,
                InvestigationStatus.HYPOTHESIZING,
                InvestigationStatus.INVESTIGATING,
                InvestigationStatus.CORRELATING,
                InvestigationStatus.PAUSED,
                InvestigationStatus.CANCELLED,
                InvestigationStatus.FAILED,
                InvestigationStatus.AWAITING_INPUT,
            },
            InvestigationStatus.CONCLUDING: {
                InvestigationStatus.COMPLETED,
                InvestigationStatus.FAILED,
                InvestigationStatus.PAUSED,
                InvestigationStatus.CANCELLED,
                InvestigationStatus.AWAITING_INPUT,
            },
            InvestigationStatus.COMPLETED: {
                InvestigationStatus.INVESTIGATING,
            },
            InvestigationStatus.FAILED: {
                InvestigationStatus.INVESTIGATING,
                InvestigationStatus.RUNNING,
            },
            InvestigationStatus.PAUSED: {
                InvestigationStatus.INVESTIGATING,
                InvestigationStatus.CANCELLED,
                InvestigationStatus.FAILED,
                InvestigationStatus.AWAITING_INPUT,
            },
            InvestigationStatus.RUNNING: {
                InvestigationStatus.CONTEXTUALIZING,
                InvestigationStatus.INVESTIGATING,
                InvestigationStatus.PAUSED,
                InvestigationStatus.CANCELLED,
                InvestigationStatus.FAILED,
                InvestigationStatus.AWAITING_INPUT,
            },
            InvestigationStatus.AWAITING_INPUT: {
                InvestigationStatus.CONTEXTUALIZING,
                InvestigationStatus.INVESTIGATING,
                InvestigationStatus.CORRELATING,
                InvestigationStatus.HYPOTHESIZING,
                InvestigationStatus.VERIFYING,
                InvestigationStatus.CONCLUDING,
                InvestigationStatus.PAUSED,
                InvestigationStatus.CANCELLED,
                InvestigationStatus.FAILED,
            },
            InvestigationStatus.CANCELLED: set(),
        }

        allowed = valid_transitions.get(self.status, set())
        if new_status not in allowed:
            raise InvalidLifecycleTransitionException(
                f"Cannot transition investigation from {self.status.value} to {new_status.value}",
                details={
                    "investigation_id": str(self.id),
                    "current_status": self.status.value,
                    "target_status": new_status.value,
                },
            )

        now = clock.utcnow()
        transition = InvestigationTransition(
            from_status=self.status,
            to_status=new_status,
            timestamp=now,
            actor=actor,
            reason=reason,
        )

        updates: dict[str, Any] = {
            "status": new_status,
            "phase": new_status.value,
            "updated_at": now,
            "version": self.version + 1,
        }
        if self.started_at is None and self.status == InvestigationStatus.CREATED:
            updates["started_at"] = now
        if new_status in {
            InvestigationStatus.COMPLETED,
            InvestigationStatus.FAILED,
            InvestigationStatus.CANCELLED,
        }:
            updates["completed_at"] = now

        new_investigation = self.model_copy(update=updates)
        return new_investigation, transition


class InvestigationState(BaseModel):
    """In-memory serializable state snapshot for execution checkpointing."""

    model_config = ConfigDict(frozen=True)

    investigation: Investigation
    active_hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=50)
    known_facts: list[Fact] = Field(default_factory=list, max_length=200)
    known_entities: list[InvestigationEntity] = Field(default_factory=list, max_length=200)
    timeline: list[TimelineEvent] = Field(default_factory=list, max_length=500)
    evidence: list[Evidence] = Field(default_factory=list, max_length=200)
    relationships: list[EvidenceRelationship] = Field(default_factory=list, max_length=500)
    pending_questions: list[str] = Field(default_factory=list, max_length=50)
    investigation_plan: list[str] = Field(default_factory=list, max_length=50)
    tool_history: list[InvestigationActionRecord] = Field(default_factory=list, max_length=100)
    findings: list[Finding] = Field(default_factory=list, max_length=50)
    llm_tokens_used: int = Field(default=0, ge=0)
    llm_cost_usd: float = Field(default=0.0, ge=0.0)
