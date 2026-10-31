# src/investigation_agent_platform/infrastructure/persistence/conclusion_unit_of_work.py
"""Postgres conclusion unit of work (H14-D1): hypothesis, finding and conclusion in one transaction."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from investigation_agent_platform.domain.finding.models import Finding, InvestigationConclusion
from investigation_agent_platform.domain.hypothesis.models import Hypothesis
from investigation_agent_platform.infrastructure.persistence.rls import rls_session


class PostgresConclusionUnitOfWork:
    """One ``rls_session`` for the conclude path's three writes."""

    def __init__(self, session_factory: Any, hypothesis_repo: Any, finding_repo: Any) -> None:
        self._factory = session_factory
        self._hypotheses = hypothesis_repo
        self._findings = finding_repo

    async def commit_verdict(
        self,
        tenant_id: str,
        *,
        investigation_id: UUID,
        hypothesis: Hypothesis | None,
        expected_version: int,
        finding: Finding,
        conclusion: InvestigationConclusion,
    ) -> None:
        async with rls_session(self._factory, tenant_id) as session:
            if hypothesis is not None:
                await self._hypotheses.save_in_session(
                    session, tenant_id, hypothesis, investigation_id, expected_version
                )
            await self._findings.save_finding_record_in_session(session, tenant_id, finding)
            await self._findings.save_conclusion_in_session(session, tenant_id, conclusion)
