# src/investigation_agent_platform/application/knowledge/intake.py
"""Error intake with code-issue merge-or-fork (Part 6 D4/D8, Slice 0).

Same code issue (tenant-agnostic fingerprint) -> annotate a new session on
the canonical investigation instead of forking a duplicate. New code issue
-> create investigation (session 1). Prior conclusions are preserved
untouched; closed targets reopen via the standard lifecycle transition.
Cancelled targets are never resurrected: a new investigation is forked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from investigation_agent_platform.domain.common.utils import Clock, SystemClock
from investigation_agent_platform.domain.investigation.models import (
    ActorType,
    Investigation,
    InvestigationContext,
    InvestigationRequest,
    InvestigationStatus,
)
from investigation_agent_platform.domain.knowledge.fingerprint import build_code_issue_fingerprint
from investigation_agent_platform.domain.knowledge.models import InvestigationSession

logger = logging.getLogger("iap.application")


@dataclass
class IntakeResult:
    investigation_id: UUID
    session_number: int
    merged: bool
    code_issue_fingerprint: str


class ErrorIntakeService:
    """Merge-or-fork intake for auto-forwarded (and analyst-reported) errors."""

    def __init__(
        self,
        investigation_repo: Any,
        session_repo: Any,
        code_issue_index: Any,
        clock: Clock | None = None,
    ) -> None:
        self.investigation_repo = investigation_repo
        self.session_repo = session_repo
        self.code_issue_index = code_issue_index
        self.clock = clock or SystemClock()

    async def intake(
        self,
        *,
        tenant_id: str,
        application_id: str,
        error_class: str,
        repository: str,
        revision: str,
        failing_symbol: str,
        caller_symbol: str | None = None,
        top_frame_file: str | None = None,
        problem_description: str = "",
        session_id: str | None = None,
        requested_by: str = "intake",
        log_refs: list[str] | None = None,
        trace_refs: list[str] | None = None,
        occurred_at: datetime | None = None,
    ) -> IntakeResult:
        now = occurred_at or datetime.now(UTC)
        fingerprint = build_code_issue_fingerprint(
            error_class=error_class,
            repository=repository,
            revision=revision,
            failing_symbol=failing_symbol,
            caller_symbol=caller_symbol,
            top_frame_file=top_frame_file,
        )

        candidate_id = await self.code_issue_index.latest_investigation(fingerprint)
        # Merge = link to the shared lineage, NOT load another tenant's row.
        # Cross-tenant rows are invisible under RLS by design, so the merge
        # target is always this tenant's own most-recent investigation for
        # the fingerprint (or a fresh row). Shared knowledge flows through
        # SHARED artifacts; session numbering stays global via the index.
        prior_rows = await self.code_issue_index.sessions_for_fingerprint(fingerprint)
        merged = len(prior_rows) > 0
        target: Investigation | None = None
        if candidate_id is not None:
            existing = await self.investigation_repo.get_by_id(tenant_id, candidate_id)
            if existing is not None and existing.status != InvestigationStatus.CANCELLED:
                target = existing
        if target is None and merged:
            # Another tenant holds the lineage; resolve this tenant's own
            # most-recent row for it (if any) via the index.
            for _, inv_id, _ in reversed(prior_rows):
                own = await self.investigation_repo.get_by_id(tenant_id, inv_id)
                if own is not None and own.status != InvestigationStatus.CANCELLED:
                    target = own
                    break

        if target is None:
            # No reusable own row (first session ever, or this tenant's rows
            # are all cancelled): fork a fresh row linked by fingerprint.
            # `merged` keeps its lineage value — joining an existing lineage
            # with a fresh row is still a merge, not a fork.
            investigation = await self._create_investigation(
                tenant_id=tenant_id,
                application_id=application_id,
                problem_description=problem_description or error_class,
                session_id=session_id or f"sess_{uuid4().hex[:12]}",
                requested_by=requested_by,
                fingerprint=fingerprint,
            )
        else:
            investigation = await self._maybe_reopen(tenant_id, target)

        session_number = await self.code_issue_index.next_session_number(fingerprint)
        session = InvestigationSession(
            investigation_id=investigation.id,
            session_number=session_number,
            tenant_id=tenant_id,
            occurred_at=now,
            log_refs=list(log_refs or []),
            trace_refs=list(trace_refs or []),
        )
        await self.session_repo.append(tenant_id, session)
        await self.code_issue_index.record_session(
            fingerprint, session_number, investigation.id, now
        )
        logger.info(
            "Intake session recorded",
            extra={
                "context": {
                    "tenant_id": tenant_id,
                    "investigation_id": str(investigation.id),
                    "session_number": session_number,
                    "merged": merged,
                }
            },
        )
        return IntakeResult(
            investigation_id=investigation.id,
            session_number=session_number,
            merged=merged,
            code_issue_fingerprint=fingerprint,
        )

    async def _create_investigation(
        self,
        *,
        tenant_id: str,
        application_id: str,
        problem_description: str,
        session_id: str,
        requested_by: str,
        fingerprint: str,
    ) -> Investigation:
        now = self.clock.utcnow()
        request = InvestigationRequest(
            application_id=application_id,
            problem_description=problem_description,
            session_id=session_id,
            requested_by=requested_by,
        )
        context = InvestigationContext(
            environment="production",
            time_window=(now, now),
        )
        investigation = Investigation(
            session_id=session_id,
            application_id=application_id,
            tenant_id=tenant_id,
            request=request,
            context=context,
            status=InvestigationStatus.CREATED,
            created_at=now,
            updated_at=now,
            code_issue_fingerprint=fingerprint,
        )
        await self.investigation_repo.create(tenant_id, investigation)
        return investigation

    async def _maybe_reopen(self, tenant_id: str, target: Investigation) -> Investigation:
        if target.status in (InvestigationStatus.COMPLETED, InvestigationStatus.FAILED):
            reopened, _ = target.transition_to(
                InvestigationStatus.INVESTIGATING,
                ActorType.SYSTEM,
                "recurring code issue",
                clock=self.clock,
            )
            await self.investigation_repo.save(tenant_id, reopened, target.version)
            return reopened
        return target
