# src/investigation_agent_platform/infrastructure/persistence/onboarding_probe_repository.py
"""Tenant-scoped persistence for adapter probe evidence and readiness reports.

Evidence is pinned to an exact request revision, scope, and policy/catalog
version at the application boundary: the repository re-validates the pin
before writing and replaces (rather than duplicates) re-probes of the same
capability. Readiness decisions are validated for internal consistency
(READY if and only if no blocking codes) before persistence. Enforcement
stays advisory in application logic; these rows are evidence, not gates.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from investigation_agent_platform.application.onboarding.adapters import (
    AdapterScope,
    ProbeEvidence,
)
from investigation_agent_platform.application.onboarding.readiness import (
    ReadinessDecision,
    ReadinessReport,
)
from investigation_agent_platform.infrastructure.persistence.models import (
    OnboardingProbeEvidenceORM,
    OnboardingReadinessReportORM,
)


def _scope_matches(scope: AdapterScope, evidence: ProbeEvidence) -> bool:
    return (
        evidence.tenant_id == scope.tenant_id
        and evidence.application_id == scope.application_id
        and evidence.environment == scope.environment
        and evidence.request_id == scope.request_id
        and evidence.revision == scope.revision
        and evidence.policy_version == scope.policy_version
        and evidence.catalog_version == scope.catalog_version
    )


class SqlAlchemyOnboardingProbeRepository:
    """Persist probe evidence and readiness reports under tenant context."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def record_evidence(
        self, scope: AdapterScope, evidence: ProbeEvidence
    ) -> OnboardingProbeEvidenceORM:
        """Insert or replace the evidence for one capability pin."""
        if not _scope_matches(scope, evidence):
            raise ValueError("probe evidence is not pinned to the given scope")
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": scope.tenant_id},
                )
                statement = (
                    insert(OnboardingProbeEvidenceORM)
                    .values(
                        tenant_id=evidence.tenant_id,
                        request_id=evidence.request_id,
                        revision=evidence.revision,
                        application_id=evidence.application_id,
                        environment=evidence.environment,
                        policy_version=evidence.policy_version,
                        catalog_version=evidence.catalog_version,
                        capability_key=evidence.capability_key,
                        adapter=evidence.adapter,
                        adapter_version=evidence.adapter_version,
                        outcome=evidence.outcome.value,
                        diagnostic_code=evidence.diagnostic_code.value,
                        detail=evidence.detail,
                        auth_mode=evidence.auth_mode,
                        probed_at=evidence.probed_at,
                        expires_at=evidence.expires_at,
                    )
                    .on_conflict_do_update(
                        constraint="uq_probe_evidence_request_capability",
                        set_={
                            "adapter": evidence.adapter,
                            "adapter_version": evidence.adapter_version,
                            "outcome": evidence.outcome.value,
                            "diagnostic_code": evidence.diagnostic_code.value,
                            "detail": evidence.detail,
                            "auth_mode": evidence.auth_mode,
                            "probed_at": evidence.probed_at,
                            "expires_at": evidence.expires_at,
                        },
                    )
                    .returning(OnboardingProbeEvidenceORM.id)
                )
                row_id = await session.scalar(statement)
                row = await session.scalar(
                    select(OnboardingProbeEvidenceORM).where(
                        OnboardingProbeEvidenceORM.id == row_id
                    )
                )
                if row is None:
                    raise RuntimeError("probe evidence write did not produce a readable row")
                return cast(OnboardingProbeEvidenceORM, row)

    async def record_report(self, report: ReadinessReport) -> OnboardingReadinessReportORM:
        """Insert or replace the readiness report for one revision and tier."""
        if (report.decision == ReadinessDecision.READY) != (len(report.blocking_codes) == 0):
            raise ValueError("readiness decision must match its blocking codes")
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": report.tenant_id},
                )
                statement = (
                    insert(OnboardingReadinessReportORM)
                    .values(
                        tenant_id=report.tenant_id,
                        request_id=report.request_id,
                        revision=report.revision,
                        tier=report.tier,
                        policy_version=report.policy_version,
                        catalog_version=report.catalog_version,
                        decision=report.decision.value,
                        blocking_codes=list(report.blocking_codes),
                        report_json=report.model_dump(mode="json"),
                        evaluated_at=report.evaluated_at,
                        expires_at=report.expires_at,
                    )
                    .on_conflict_do_update(
                        constraint="uq_readiness_report_request_tier",
                        set_={
                            "policy_version": report.policy_version,
                            "catalog_version": report.catalog_version,
                            "decision": report.decision.value,
                            "blocking_codes": list(report.blocking_codes),
                            "report_json": report.model_dump(mode="json"),
                            "evaluated_at": report.evaluated_at,
                            "expires_at": report.expires_at,
                        },
                    )
                    .returning(OnboardingReadinessReportORM.id)
                )
                row_id = await session.scalar(statement)
                row = await session.scalar(
                    select(OnboardingReadinessReportORM).where(
                        OnboardingReadinessReportORM.id == row_id
                    )
                )
                if row is None:
                    raise RuntimeError("readiness report write did not produce a readable row")
                return cast(OnboardingReadinessReportORM, row)

    async def evidence_for(
        self, scope: AdapterScope, capability_key: str
    ) -> OnboardingProbeEvidenceORM | None:
        """Latest evidence for one capability pin, or None."""
        async with self._session_factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": scope.tenant_id},
            )
            return cast(
                OnboardingProbeEvidenceORM | None,
                await session.scalar(
                    select(OnboardingProbeEvidenceORM).where(
                        OnboardingProbeEvidenceORM.tenant_id == scope.tenant_id,
                        OnboardingProbeEvidenceORM.request_id == scope.request_id,
                        OnboardingProbeEvidenceORM.revision == scope.revision,
                        OnboardingProbeEvidenceORM.capability_key == capability_key,
                    )
                ),
            )

    async def list_evidence_for(self, scope: AdapterScope) -> list[OnboardingProbeEvidenceORM]:
        """All stored probe evidences for one request pin (DEP-07 analysis input)."""
        async with self._session_factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": scope.tenant_id},
            )
            result = await session.scalars(
                select(OnboardingProbeEvidenceORM).where(
                    OnboardingProbeEvidenceORM.tenant_id == scope.tenant_id,
                    OnboardingProbeEvidenceORM.request_id == scope.request_id,
                    OnboardingProbeEvidenceORM.revision == scope.revision,
                )
            )
            return list(result.all())

    async def report_for(
        self, tenant_id: str, request_id: UUID, revision: int, tier: str
    ) -> OnboardingReadinessReportORM | None:
        """Latest readiness report for one revision and tier, or None."""
        async with self._session_factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": tenant_id},
            )
            return cast(
                OnboardingReadinessReportORM | None,
                await session.scalar(
                    select(OnboardingReadinessReportORM).where(
                        OnboardingReadinessReportORM.tenant_id == tenant_id,
                        OnboardingReadinessReportORM.request_id == request_id,
                        OnboardingReadinessReportORM.revision == revision,
                        OnboardingReadinessReportORM.tier == tier,
                    )
                ),
            )
