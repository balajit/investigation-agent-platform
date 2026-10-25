# src/investigation_agent_platform/application/investigation/services.py
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from investigation_agent_platform.domain.common.exceptions import (
    ApplicationProfileNotFoundException,
    DomainException,
    SecurityPolicyViolationException,
)
from investigation_agent_platform.domain.common.utils import Clock, SystemClock
from investigation_agent_platform.domain.entity.models import InvestigationEntity
from investigation_agent_platform.domain.events.base import InvestigationCancelled
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceRelationship
from investigation_agent_platform.domain.finding.models import Finding
from investigation_agent_platform.domain.hypothesis.models import Hypothesis
from investigation_agent_platform.domain.investigation.models import (
    ActorType,
    Fact,
    Investigation,
    InvestigationAction,
    InvestigationContext,
    InvestigationLimits,
    InvestigationRequest,
    InvestigationStatus,
)
from investigation_agent_platform.domain.profile.models import ApplicationProfile
from investigation_agent_platform.domain.timeline.models import TimelineEvent
from investigation_agent_platform.infrastructure.observability.tracing import traced
from investigation_agent_platform.ports.messaging.publisher import EventPublisher
from investigation_agent_platform.ports.observability.telemetry import ObservabilityPort
from investigation_agent_platform.ports.persistence.repositories import (
    ActionExecutionRepository,
    ApplicationProfileRepository,
    CheckpointRepository,
    EvidenceRepository,
    FindingConclusionRepository,
    InvestigationRepository,
    TimelineRepository,
    TransitionEventRepository,
)
from investigation_agent_platform.ports.security.redactor import (
    ActionAuthorizerPort,
    CapabilityRegistryPort,
)

logger = logging.getLogger("iap.application")


def _create_extra(investigation: Any) -> dict[str, Any]:
    """Create outcome: status + disposition action (never problem text)."""
    try:
        extra: dict[str, Any] = {}
        status = getattr(investigation, "status", None)
        if status is not None:
            extra["iap.status"] = str(getattr(status, "value", status))
        metadata = getattr(investigation, "metadata", None) or {}
        disposition = metadata.get("disposition") if isinstance(metadata, dict) else None
        if isinstance(disposition, dict) and disposition.get("action"):
            extra["iap.disposition"] = str(disposition["action"])
        return extra
    except Exception:
        return {}


def _authorize_extra(decision: Any) -> dict[str, Any]:
    """Auth decision shape only (never action parameters)."""
    try:
        if isinstance(decision, dict):
            authorized = decision.get("authorized", decision.get("allowed"))
            return {"iap.authorized": bool(authorized)} if authorized is not None else {}
        authorized = getattr(decision, "authorized", getattr(decision, "allowed", None))
        return {"iap.authorized": bool(authorized)} if authorized is not None else {}
    except Exception:
        return {}


@dataclass
class InvestigationExecutionContext:
    investigation: Investigation
    profile: ApplicationProfile
    facts: list[Fact] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    entities: list[InvestigationEntity] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)
    relationships: list[EvidenceRelationship] = field(default_factory=list)
    execution_limits: InvestigationLimits = field(default_factory=InvestigationLimits)


class CreateInvestigationService:
    def __init__(
        self,
        investigation_repo: InvestigationRepository,
        profile_repo: ApplicationProfileRepository,
        telemetry: ObservabilityPort | None = None,
        clock: Clock | None = None,
        # §2 disposition ports (all optional; absent → disposition degrades
        # gracefully: links skipped, re-verify checks reported as skipped).
        code_issue_index: object | None = None,
        finding_repo: FindingConclusionRepository | None = None,
        evidence_repo: EvidenceRepository | None = None,
        contradiction_repo: object | None = None,
        hypothesis_repo: object | None = None,
        relationship_repo: object | None = None,
        code_reader: object | None = None,
        probe_runner: object | None = None,
    ) -> None:
        self.investigation_repo = investigation_repo
        self.profile_repo = profile_repo
        self.telemetry = telemetry
        self.clock = clock or SystemClock()
        self.code_issue_index = code_issue_index
        self.finding_repo = finding_repo
        self.evidence_repo = evidence_repo
        self.contradiction_repo = contradiction_repo
        self.hypothesis_repo = hypothesis_repo
        self.relationship_repo = relationship_repo
        self.code_reader = code_reader
        self.probe_runner = probe_runner

    @traced("reason", component="create_service", extra_from=_create_extra)
    async def execute(
        self,
        request: InvestigationRequest,
        tenant_id: str,
        skip_disposition: bool = False,
    ) -> Investigation:
        """Create an investigation. `skip_disposition=True` bypasses §2
        matching/linking entirely — reserved for explicit operator actions
        (reprocess versioning), never for new requests."""
        logger.info(
            "Creating investigation",
            extra={"context": {"application_id": request.application_id, "tenant_id": tenant_id}},
        )

        profile = await self.profile_repo.get_by_application_id(tenant_id, request.application_id)
        if not profile:
            raise ApplicationProfileNotFoundException(
                f"Application profile {request.application_id} not found",
                details={"application_id": request.application_id},
            )
        # Lifecycle gate (§4): retired profiles serve in-flight runs but block
        # new creates. Resolve the latest ACTIVE revision for content.
        revisions = await self.profile_repo.list_revisions(tenant_id, request.application_id)
        if revisions and revisions[0].status.value in ("DEPRECATED", "ARCHIVED"):
            from investigation_agent_platform.domain.common.exceptions import (
                InvalidLifecycleTransitionException,
            )

            raise InvalidLifecycleTransitionException(
                f"Application profile {request.application_id} is {revisions[0].status.value}; "
                "new investigations are blocked.",
                details={"application_id": request.application_id},
            )
        for rev in revisions:
            if rev.status.value == "ACTIVE":
                profile = rev
                break
        else:
            raise ApplicationProfileNotFoundException(
                f"No ACTIVE revision for application profile {request.application_id}",
                details={"application_id": request.application_id},
            )

        now = self.clock.utcnow()
        window_seconds = profile.investigation.default_time_window
        time_window = (now - timedelta(seconds=window_seconds), now)

        context = InvestigationContext(
            environment=profile.environment,
            time_window=time_window,
            known_identifiers={"sessionId": request.session_id} if request.session_id else {},
        )

        # §2 similar-past-investigation disposition (deterministic, pre-run,
        # fail-open to FRESH). Fingerprint reuses the error-intake identity
        # when the caller supplies it via parameters — never a second identity.
        # Reprocess versioning bypasses it entirely: an explicit operator
        # restart must never be triaged as a duplicate, linked, or cancelled.
        from investigation_agent_platform.application.investigation.disposition import (
            Disposition,
            DispositionAction,
            disposition_for_request,
        )

        raw_fingerprint = (request.parameters or {}).get("code_issue_fingerprint")
        fingerprint = str(raw_fingerprint).strip() or None if raw_fingerprint is not None else None
        declined = bool((request.parameters or {}).get("declined_as_not_needed", False))
        if skip_disposition:
            disposition = Disposition(
                action=DispositionAction.FRESH,
                start_run=True,
                primary_target_id=None,
                primary_link=None,
                reason="disposition bypassed: explicit operator action (reprocess)",
            )
        else:
            disposition = await disposition_for_request(
                tenant_id=tenant_id,
                application_id=request.application_id,
                problem_description=request.problem_description,
                fingerprint=fingerprint,
                investigation_repo=self.investigation_repo,
                finding_repo=self.finding_repo,
                code_issue_index=self.code_issue_index,
                evidence_repo=self.evidence_repo,
                contradiction_repo=self.contradiction_repo,
                hypothesis_repo=self.hypothesis_repo,
                code_reader=self.code_reader,
                probe_runner=self.probe_runner,
                declined=declined,
            )

        status = InvestigationStatus.CREATED
        completed_at = None
        if not disposition.start_run:
            # No new run: the record closes as CANCELLED at create. The start
            # gate (CREATED/FAILED/RUNNING only) then structurally blocks any
            # later workflow dispatch — the full budget is saved by construction.
            status = InvestigationStatus.CANCELLED
            completed_at = now

        investigation = Investigation(
            session_id=request.session_id,
            application_id=request.application_id,
            tenant_id=tenant_id,
            request=request,
            context=context,
            status=status,
            created_at=now,
            updated_at=now,
            completed_at=completed_at,
            version=1,
            code_issue_fingerprint=fingerprint,
            # Part 11.2: pin the exact profile revision this investigation
            # was created against, for reproducibility as profiles evolve.
            metadata={
                "profile_version": profile.version,
                "disposition": disposition.to_metadata(),
            },
        )

        await self.investigation_repo.create(tenant_id, investigation)

        # §2.4: links written by the disposition step, never by the LLM.
        # Fail-open: link failures never break the create.
        if disposition.primary_target_id is not None and disposition.primary_link is not None:
            try:
                await self._write_disposition_links(tenant_id, investigation, disposition)
            except Exception as exc:
                logger.warning(
                    "Disposition links skipped; investigation stands",
                    extra={"context": {"investigation_id": str(investigation.id), "error": str(exc)}},
                )
        if self.telemetry:
            self.telemetry.record_metric(
                "investigation_created", 1.0, {"app_id": request.application_id}
            )

        return investigation

    async def _write_disposition_links(
        self, tenant_id: str, investigation: Investigation, disposition: object
    ) -> None:
        """Persist §2.4 investigation-level links idempotently."""
        repo = self.relationship_repo
        if repo is None or not hasattr(repo, "record_edges"):
            return
        from investigation_agent_platform.application.investigation.disposition import (
            RELATES_TO,
            investigation_node,
        )
        from investigation_agent_platform.ports.persistence.repositories import (
            EvidenceRelationshipRecord,
        )

        edges = [
            EvidenceRelationshipRecord(
                tenant_id=tenant_id,
                application_id=investigation.application_id,
                source_id=investigation_node(investigation.id),
                target_id=investigation_node(disposition.primary_target_id),
                type=str(disposition.primary_link),
                confidence=1.0 if disposition.matched_by == "fingerprint" else 0.6,
                metadata={
                    "reason": disposition.reason,
                    "provenance_evidence_id": "",
                    "written_by": "disposition",
                },
            )
        ]
        for other_id in disposition.cluster_link_ids:
            edges.append(
                EvidenceRelationshipRecord(
                    tenant_id=tenant_id,
                    application_id=investigation.application_id,
                    source_id=investigation_node(investigation.id),
                    target_id=investigation_node(other_id),
                    type=RELATES_TO,
                    confidence=0.5,
                    metadata={"reason": "similar-past-investigation cluster", "provenance_evidence_id": "", "written_by": "disposition"},
                )
            )
        await repo.record_edges(tenant_id, investigation.id, investigation.application_id, edges)


class GetInvestigationService:
    def __init__(self, investigation_repo: InvestigationRepository) -> None:
        self.investigation_repo = investigation_repo

    async def execute(self, tenant_id: str, investigation_id: UUID) -> Investigation | None:
        return await self.investigation_repo.get_by_id(tenant_id, investigation_id)


class ResumeInvestigationService:
    def __init__(
        self,
        investigation_repo: InvestigationRepository,
        profile_repo: ApplicationProfileRepository,
        evidence_repo: EvidenceRepository,
        timeline_repo: TimelineRepository,
        checkpoint_repo: CheckpointRepository,
    ) -> None:
        self.investigation_repo = investigation_repo
        self.profile_repo = profile_repo
        self.evidence_repo = evidence_repo
        self.timeline_repo = timeline_repo
        self.checkpoint_repo = checkpoint_repo

    async def execute(
        self, tenant_id: str, investigation_id: UUID
    ) -> InvestigationExecutionContext:
        investigation = await self.investigation_repo.get_by_id(tenant_id, investigation_id)
        if not investigation:
            raise DomainException(
                f"Investigation {investigation_id} not found", error_code="NOT_FOUND"
            )

        profile = await self.profile_repo.get_by_application_id(
            tenant_id, investigation.application_id
        )
        if not profile:
            raise ApplicationProfileNotFoundException(
                f"Profile {investigation.application_id} not found"
            )

        evidence = await self.evidence_repo.find_by_investigation_id(tenant_id, investigation_id)
        timeline = await self.timeline_repo.find_by_investigation_id(tenant_id, investigation_id)

        return InvestigationExecutionContext(
            investigation=investigation,
            profile=profile,
            evidence=evidence,
            timeline=timeline,
        )


class CancelInvestigationService:
    def __init__(
        self,
        investigation_repo: InvestigationRepository,
        transition_repo: TransitionEventRepository,
        event_publisher: EventPublisher | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.investigation_repo = investigation_repo
        self.transition_repo = transition_repo
        self.event_publisher = event_publisher
        self.clock = clock or SystemClock()

    async def execute(
        self, tenant_id: str, investigation_id: UUID, reason: str, actor: ActorType = ActorType.USER
    ) -> Investigation:
        investigation = await self.investigation_repo.get_by_id(tenant_id, investigation_id)
        if not investigation:
            raise DomainException(
                f"Investigation {investigation_id} not found", error_code="NOT_FOUND"
            )

        updated_inv, transition = investigation.transition_to(
            new_status=InvestigationStatus.CANCELLED,
            actor=actor,
            reason=reason,
        )

        await self.investigation_repo.save(
            tenant_id, updated_inv, expected_version=investigation.version
        )
        await self.transition_repo.record_transition(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            from_state=transition.from_status.value,
            to_state=transition.to_status.value,
            reason=reason,
        )

        if self.event_publisher:
            event = InvestigationCancelled(
                tenant_id=tenant_id,
                investigation_id=updated_inv.id,
                application_id=updated_inv.application_id,
                correlation_id=updated_inv.id,
                reason=reason,
                cancelled_by=actor.value,
            )
            await self.event_publisher.publish_domain_event(tenant_id, event)

        return updated_inv


class ExecuteActionService:
    """Mandatory authorization gate for every agent-proposed action (F-004).

    No action reaches the evidence gateway or is recorded as executed unless
    ``authorizer.authorize_action`` explicitly returns ``True`` and the
    requested capability is enabled for the tenant. Both dependencies are
    mandatory — construction without them, or a missing decision, denies by
    default rather than silently allowing execution.
    """

    def __init__(
        self,
        action_repo: ActionExecutionRepository,
        evidence_repo: EvidenceRepository,
        authorizer: ActionAuthorizerPort,
        capability_registry: CapabilityRegistryPort,
        policy_version: str = "v1",
    ) -> None:
        if authorizer is None or capability_registry is None:
            raise SecurityPolicyViolationException(
                "ExecuteActionService requires an authorizer and capability registry"
            )
        self.action_repo = action_repo
        self.evidence_repo = evidence_repo
        self.authorizer = authorizer
        self.capability_registry = capability_registry
        self.policy_version = policy_version

    @traced("reason", component="action_service", extra_from=_authorize_extra)
    async def authorize(
        self,
        tenant_id: str,
        investigation_id: UUID,
        action: InvestigationAction,
        principal_id: str,
        application_id: str | None = None,
    ) -> dict[str, object]:
        """Evaluate the mandatory authorization gate and return the recorded decision.

        Raises ``SecurityPolicyViolationException`` (non-retryable) if the
        action is not explicitly authorized or the capability is disabled.
        Never returns a permissive result on authorizer failure.
        """
        target_resource = str(action.parameters.get("resource", ""))
        context = {
            "investigation_id": str(investigation_id),
            "principal_id": principal_id,
            "action_id": str(action.action_id),
            "application_id": application_id,
        }
        authorized = await self.authorizer.authorize_action(
            tenant_id, action.action_type.value, target_resource, context
        )
        if not authorized:
            raise SecurityPolicyViolationException(
                f"Action '{action.action_type.value}' denied by authorization policy",
                details={"tenant_id": tenant_id, "action_type": action.action_type.value},
            )
        enabled = await self.capability_registry.is_capability_enabled(
            tenant_id, action.action_type.value
        )
        if not enabled:
            raise SecurityPolicyViolationException(
                f"Capability '{action.action_type.value}' is disabled for tenant",
                details={"tenant_id": tenant_id, "action_type": action.action_type.value},
            )
        return {
            "authorized": True,
            "policy_version": self.policy_version,
            "action_type": action.action_type.value,
            "principal_id": principal_id,
        }

    async def execute(
        self,
        tenant_id: str,
        investigation_id: UUID,
        action: InvestigationAction,
        principal_id: str = "worker",
    ) -> None:
        await self.action_repo.record_action(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            action=action,
            result_status="EXECUTED",
            principal_id=principal_id,
            policy_version=self.policy_version,
        )


class ConcludeInvestigationService:
    def __init__(
        self,
        investigation_repo: InvestigationRepository,
        finding_repo: FindingConclusionRepository,
    ) -> None:
        self.investigation_repo = investigation_repo
        self.finding_repo = finding_repo

    async def execute(self, tenant_id: str, investigation_id: UUID, finding: Finding) -> None:
        await self.finding_repo.save_finding(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            finding_type=finding.finding_type.value,
            details=finding.model_dump(),
        )


class ReprocessMode(str):
    """Reprocess versioning modes (endpoint-validated strings)."""

    RESTART_FRESH = "RESTART_FRESH"
    RESTART_CONTINUE = "RESTART_CONTINUE"


TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})

# Multiplier bounds: int in [1, 10]; tokens hard-capped at 1M per run.
MIN_BUDGET_MULTIPLIER = 1
MAX_BUDGET_MULTIPLIER = 10
MAX_RUN_LLM_TOKENS = 1_000_000
MAX_RUN_ITERATIONS = 100

# InvestigationLimits fields the multiplier may scale (tokens handled
# separately via the absolute ceiling).
_SCALABLE_LIMIT_FIELDS = (
    "max_iterations",
    "max_tool_calls",
    "max_evidence_items",
    "max_hypotheses",
)


def require_budget_approver(principal_id: str, multiplier: int) -> dict[str, Any]:
    """Approval seam for multiplied budgets (future-proof for role gating).

    No roles exist today: every authenticated principal may approve, and the
    approval is recorded for audit. When roles land, tighten THIS function
    (e.g. multiplier > 2 requires owner) — callers already branch on denial
    via BudgetApprovalDeniedException, so no other code changes.
    """
    return {
        "approved_by": principal_id,
        "multiplier": multiplier,
        "approved_at": datetime.now(UTC).isoformat(),
        "policy": "permissive-no-roles",
    }


class BudgetApprovalDeniedException(Exception):
    """Raised by require_budget_approver once role gating lands (reserved)."""


def apply_budget_multiplier(base_limits: dict[str, Any], multiplier: int) -> dict[str, Any]:
    """Scale base limits by N with the absolute token ceiling. Pure.

    Tokens: min(base × N, 1_000_000) — the ceiling is per-run absolute.
    Iterations saturate at the domain le=100 clamp, never violate it.
    Unknown/non-positive-int fields are ignored (fail-closed to base).
    """
    if not isinstance(multiplier, int) or isinstance(multiplier, bool):
        from investigation_agent_platform.domain.common.exceptions import (
            DomainValidationException as DomainValidationError,
        )

        raise DomainValidationError(
            f"budget_multiplier must be an int in [1, 10], got {multiplier!r}",
            details={"budget_multiplier": multiplier},
        )
    if not MIN_BUDGET_MULTIPLIER <= multiplier <= MAX_BUDGET_MULTIPLIER:
        from investigation_agent_platform.domain.common.exceptions import (
            DomainValidationException as DomainValidationError,
        )

        raise DomainValidationError(
            f"budget_multiplier must be an int in [{MIN_BUDGET_MULTIPLIER}, "
            f"{MAX_BUDGET_MULTIPLIER}], got {multiplier!r}",
            details={"budget_multiplier": multiplier},
        )
    effective: dict[str, Any] = {}
    for limit_field in _SCALABLE_LIMIT_FIELDS:
        value = base_limits.get(limit_field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            scaled = value * multiplier
            if limit_field == "max_iterations":
                scaled = min(scaled, MAX_RUN_ITERATIONS)
            effective[limit_field] = scaled
    tokens = base_limits.get("max_llm_tokens")
    if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 1:
        effective["max_llm_tokens"] = min(tokens * multiplier, MAX_RUN_LLM_TOKENS)
    else:
        effective["max_llm_tokens"] = MAX_RUN_LLM_TOKENS
    return effective


class ReprocessInvestigationService:
    """Version-per-reprocess: terminal rows are never mutated.

    RESTART_FRESH always creates a new investigation version (fresh request
    copy, no artifacts). RESTART_CONTINUE on a terminal original also creates
    a new version but copies gathered artifacts (evidence re-keyed +
    re-fingerprinted, hypotheses re-IDed with VERIFIED demoted, supporting
    refs remapped). RESTART_CONTINUE on an active original is rejected —
    redispatch the live run instead. Disposition matching/linking is NEVER
    invoked: restarts are explicit operator actions, not triaged requests;
    lineage travels in metadata only.
    """

    def __init__(
        self,
        investigation_repo: InvestigationRepository,
        profile_repo: ApplicationProfileRepository,
        evidence_repo: Any | None = None,
        hypothesis_repo: Any | None = None,
        finding_repo: FindingConclusionRepository | None = None,
        checkpoint_repo: Any | None = None,
        create_service: CreateInvestigationService | None = None,
        telemetry: ObservabilityPort | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.investigation_repo = investigation_repo
        self.profile_repo = profile_repo
        self.evidence_repo = evidence_repo
        self.hypothesis_repo = hypothesis_repo
        self.finding_repo = finding_repo
        self.checkpoint_repo = checkpoint_repo
        self.create_service = create_service
        self.telemetry = telemetry
        self.clock = clock or SystemClock()

    async def execute(
        self,
        tenant_id: str,
        investigation_id: UUID,
        mode: str,
        budget_multiplier: int = 1,
        principal_id: str = "api-user",
        reason: str = "",
    ) -> Investigation:
        from investigation_agent_platform.domain.common.exceptions import (
            InvalidInvestigationStateException,
            InvalidLifecycleTransitionException,
        )

        if mode not in (ReprocessMode.RESTART_FRESH, ReprocessMode.RESTART_CONTINUE):
            raise InvalidLifecycleTransitionException(
                f"Unknown reprocess mode {mode!r}; expected RESTART_FRESH or RESTART_CONTINUE",
                details={"investigation_id": str(investigation_id)},
            )
        original = await self.investigation_repo.get_by_id(tenant_id, investigation_id)
        if original is None or getattr(original, "tenant_id", tenant_id) != tenant_id:
            from investigation_agent_platform.domain.common.exceptions import (
                DomainException,
            )

            raise DomainException(
                f"Investigation {investigation_id} not found",
                error_code="INVESTIGATION_NOT_FOUND",
                details={"investigation_id": str(investigation_id)},
                retryable=False,
                http_status_code=404,
            )
        status = original.status.value if hasattr(original.status, "value") else str(original.status)
        is_terminal = status in TERMINAL_STATUSES
        if not is_terminal and mode == ReprocessMode.RESTART_CONTINUE:
            raise InvalidInvestigationStateException(
                f"RESTART_CONTINUE requires a terminal investigation (status is {status}); "
                "redispatch the live run instead",
                details={"investigation_id": str(investigation_id), "status": status},
            )
        approval = require_budget_approver(principal_id, budget_multiplier)
        effective_limits = await self._effective_limits(tenant_id, original, budget_multiplier)
        prior_spend = await self._prior_spend(tenant_id, original)

        prior_snapshot = {
            "investigation_id": str(original.id),
            "status": status,
            "conclusion": self._serializable(getattr(original, "conclusion", None)),
            "tokens_spent": prior_spend,
            "mode": mode,
            "reason": reason,
        }
        prior_runs = list((original.metadata or {}).get("prior_runs", []) or [])
        prior_runs.append(prior_snapshot)
        version = len(prior_runs) + 1

        request = original.request.model_copy(
            update={
                "session_id": f"{original.session_id}-r{version}"[:128],
                "requested_by": principal_id,
                "requested_at": self.clock.utcnow(),
            }
        )
        create = self.create_service or CreateInvestigationService(
            investigation_repo=self.investigation_repo,
            profile_repo=self.profile_repo,
            telemetry=self.telemetry,
            clock=self.clock,
        )
        new_investigation = await create.execute(request, tenant_id, skip_disposition=True)

        metadata = dict(new_investigation.metadata or {})
        # D2: intake seed is incident identity, not run state — carry it
        # forward so reprocessed runs keep frames/refs/severity/golden
        # pairs (RESTART_FRESH otherwise silently drops the seed and the
        # stack-walk forcing never fires).
        try:
            original_seed = (original.metadata or {}).get("intake_seed", None)
            if isinstance(original_seed, dict) and original_seed:
                metadata["intake_seed"] = dict(original_seed)
        except Exception:
            pass
        metadata.update(
            {
                "reprocessed_from": str(original.id),
                "reprocess_mode": mode,
                "reprocess_version": version,
                "prior_runs": prior_runs,
                "effective_limits": effective_limits,
                "budget_approval": approval,
            }
        )
        new_investigation = new_investigation.model_copy(update={"metadata": metadata})
        await self.investigation_repo.save(tenant_id, new_investigation, expected_version=1)
        # Re-read so downstream dispatch saves against the bumped version.
        refreshed = await self.investigation_repo.get_by_id(tenant_id, new_investigation.id)
        if refreshed is not None:
            new_investigation = refreshed

        if mode == ReprocessMode.RESTART_CONTINUE:
            await self._copy_artifacts(tenant_id, original, new_investigation)

        if self.telemetry:
            self.telemetry.record_metric(
                "investigation_reprocessed",
                1.0,
                {"app_id": new_investigation.application_id, "mode": mode},
            )
        return new_investigation

    async def _effective_limits(
        self, tenant_id: str, original: Investigation, multiplier: int
    ) -> dict[str, Any]:
        """Base = InvestigationLimits defaults overlaid by profile budget (when
        resolvable), then scaled. Fail-open to scaled defaults."""
        from investigation_agent_platform.domain.investigation.models import (
            InvestigationLimits,
        )

        base: dict[str, Any] = InvestigationLimits().model_dump()
        if self.profile_repo is not None:
            profile = None
            try:
                profile = await self.profile_repo.get_by_application_id(
                    tenant_id, original.application_id
                )
            except Exception:
                profile = None
            if profile is not None:
                investigation_cfg = getattr(profile, "investigation", None)
                budget = {}
                if investigation_cfg is not None:
                    budget = getattr(investigation_cfg, "evidence_budget", None) or {}
                    if isinstance(budget, dict):
                        for field in _SCALABLE_LIMIT_FIELDS + ("max_llm_tokens",):
                            value = budget.get(field)
                            if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
                                base[field] = value
        return apply_budget_multiplier(base, multiplier)

    async def _prior_spend(self, tenant_id: str, original: Investigation) -> int:
        """Tokens spent by the original run (latest checkpoint), best-effort."""
        if self.checkpoint_repo is None:
            return 0
        try:
            checkpoint = await self.checkpoint_repo.get_latest_checkpoint(
                tenant_id, original.id
            )
        except Exception:
            return 0
        if not isinstance(checkpoint, dict):
            snapshot = getattr(checkpoint, "state_snapshot", None)
            checkpoint = snapshot if isinstance(snapshot, dict) else {}
        try:
            return int(checkpoint.get("llm_tokens_used", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _serializable(value: Any) -> Any:
        try:
            if value is None:
                return None
            if hasattr(value, "model_dump"):
                return value.model_dump(mode="json")
            return str(value)[:2000]
        except Exception:
            return None

    async def _copy_artifacts(
        self, tenant_id: str, original: Investigation, new_investigation: Investigation
    ) -> None:
        """Copy evidence + hypotheses onto the new version (G6 sharing).

        Reference rows (code/history) keep their ORIGINAL fingerprints: persistence
        records bank-interest on the canonical rows instead of duplicating
        content. Observational rows are per-investigation by nature, so their
        copies get re-salted fingerprints (true new observations, not aliases).
        New UUIDs per copy keep identity distinct; provenance is re-pointed at
        the new row. Hypotheses get new IDs, VERIFIED demoted to
        UNDER_INVESTIGATION (never carried), supporting/refuting refs remapped
        through the old→new evidence map, assessments cleared (they cite
        old-row ranges). Conclusions/findings are terminal records and are NOT
        copied — they live in metadata.prior_runs.
        """
        import hashlib as _hashlib

        from investigation_agent_platform.domain.evidence.models import (
            REFERENCE_LIFECYCLE_TYPES,
        )

        evidence_map: dict[str, str] = {}
        if self.evidence_repo is not None and hasattr(
            self.evidence_repo, "find_by_investigation_id"
        ):
            try:
                rows = await self.evidence_repo.find_by_investigation_id(
                    tenant_id, original.id
                )
            except Exception:
                rows = []
            copies = []
            for row in rows or []:
                old_id = str(getattr(row, "evidence_id", "") or "")
                new_id = uuid4()
                if old_id:
                    evidence_map[old_id] = str(new_id)
                etype = getattr(row, "evidence_type", "")
                etype_name = str(getattr(etype, "value", etype) or "")
                updates: dict[str, Any] = {
                    "evidence_id": new_id,
                    "investigation_id": new_investigation.id,
                }
                if etype_name not in REFERENCE_LIFECYCLE_TYPES:
                    # Observational copies are new observations: re-salt so
                    # they persist as own rows instead of aliasing.
                    fingerprint = getattr(row, "fingerprint", "") or ""
                    updates["fingerprint"] = _hashlib.sha256(
                        f"{fingerprint}\x1f{new_investigation.id}".encode()
                    ).hexdigest()
                provenance = getattr(row, "provenance", None)
                if provenance is not None and hasattr(provenance, "model_copy"):
                    try:
                        provenance = provenance.model_copy(
                            update={"investigation_id": new_investigation.id}
                        )
                    except Exception:
                        pass
                try:
                    updates["provenance"] = provenance
                    copies.append(row.model_copy(update=updates))
                except Exception:
                    continue
            if copies and hasattr(self.evidence_repo, "save_batch"):
                try:
                    await self.evidence_repo.save_batch(
                        tenant_id, copies, new_investigation.id
                    )
                except Exception:
                    pass

        if self.hypothesis_repo is not None and hasattr(
            self.hypothesis_repo, "find_by_investigation_id"
        ):
            try:
                hyps = await self.hypothesis_repo.find_by_investigation_id(
                    tenant_id, original.id
                )
            except Exception:
                hyps = []
            for hyp in hyps or []:
                try:
                    status = getattr(hyp, "status", None)
                    status_value = getattr(status, "value", status)
                    updates: dict[str, Any] = {
                        "id": uuid4(),
                        "investigation_id": new_investigation.id,
                    }
                    if str(status_value) == "VERIFIED":
                        from investigation_agent_platform.domain.hypothesis.models import (
                            HypothesisStatus,
                        )

                        updates["status"] = HypothesisStatus.UNDER_INVESTIGATION
                    # Tier 1: prune, don't dangle. IDs with no copied
                    # counterpart (purged rows) are dropped instead of
                    # carried as stale pointers that _resolve silently
                    # discards at verify time.
                    remapped_supporting = [
                        evidence_map[str(eid)]
                        for eid in (getattr(hyp, "supporting_evidence_ids", []) or [])
                        if str(eid) in evidence_map
                    ]
                    remapped_refuting = [
                        evidence_map[str(eid)]
                        for eid in (getattr(hyp, "refuting_evidence_ids", []) or [])
                        if str(eid) in evidence_map
                    ]
                    updates["supporting_evidence_ids"] = remapped_supporting
                    updates["refuting_evidence_ids"] = remapped_refuting
                    if hasattr(hyp, "assessments"):
                        updates["assessments"] = []
                    await self.hypothesis_repo.save(
                        tenant_id,
                        hyp.model_copy(update=updates),
                        new_investigation.id,
                        0,
                    )
                except Exception:
                    continue
