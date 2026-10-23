# src/investigation_agent_platform/infrastructure/persistence/onboarding_deletion_repository.py
"""Persistence for the deletion store registry, manifests, approvals, outcomes.

Manifests are immutable once previewed; approvals carry only digests and are
consumed atomically once (conditional write, never an application-memory
``used`` flag); outcomes are append-only per-resource records. Terminal
states are computed from outcomes, never asserted by callers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from investigation_agent_platform.application.onboarding.deletion import (
    ApprovalPayload,
    DeletionStatus,
    canonical_approval_digest,
    mint_approval_token,
)
from investigation_agent_platform.infrastructure.persistence.models import (
    OnboardingDeletionApprovalORM,
    OnboardingDeletionManifestORM,
    OnboardingDeletionOutcomeORM,
    OnboardingStoreRegistryORM,
)
from investigation_agent_platform.infrastructure.persistence.rls import (
    rls_session,
)


class DeletionApprovalError(ValueError):
    """Approval binding, expiry, replay, or concurrency failure."""


TERMINAL_COMPLETE = DeletionStatus.COMPLETE.value
TERMINAL_RETENTION = DeletionStatus.COMPLETE_WITH_RETENTION.value
TERMINAL_RESIDUALS = DeletionStatus.COMPLETE_WITH_APPROVED_RESIDUALS.value


def evaluate_terminal_state(outcome_states: list[str]) -> str:
    """Compute the honest terminal state from per-entry outcomes.

    Unknown or missing entries are nonterminal by construction: callers pass
    only recorded states, and an empty list is INCOMPLETE, never COMPLETE.
    """
    if not outcome_states:
        return DeletionStatus.INCOMPLETE.value
    states = set(outcome_states)
    if states <= {"DONE"}:
        return TERMINAL_COMPLETE
    if states <= {"DONE", "RETAINED_BY_POLICY"}:
        return TERMINAL_RETENTION
    if states <= {"DONE", "RETAINED_BY_POLICY", "APPROVED_RESIDUAL"}:
        return TERMINAL_RESIDUALS
    if "BLOCKED" in states:
        return DeletionStatus.BLOCKED.value
    if "CANCELED" in states:
        return DeletionStatus.CANCELED.value
    return DeletionStatus.INCOMPLETE.value


class SqlAlchemyOnboardingDeletionRepository:
    """Registry, manifest, approval, and outcome persistence."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def record_registry_entries(
        self, *, deployment_id: str, registry_version: str, entries: list[dict[str, Any]]
    ) -> int:
        """Idempotent operator registration of one registry version."""
        if not deployment_id or not registry_version:
            raise ValueError("deployment_id and registry_version are required")
        async with self._session_factory() as session:
            async with session.begin():
                count = 0
                for entry in entries:
                    statement = (
                        insert(OnboardingStoreRegistryORM)
                        .values(
                            deployment_id=deployment_id,
                            store_id=entry["store_id"],
                            store_type=entry["store_type"],
                            adapter_version=entry["adapter_version"],
                            registry_version=registry_version,
                            required_for_deletion=entry.get("required_for_deletion", True),
                            enabled=entry.get("enabled", True),
                            capabilities_json=entry.get("capabilities_json", {}),
                        )
                        .on_conflict_do_nothing(constraint="uq_store_registry_version")
                    )
                    result = await session.execute(statement)
                    count += result.rowcount or 0
                return count

    async def save_manifest(
        self,
        *,
        tenant_id: str,
        manifest_id: UUID,
        application_id: str | None,
        environment: str,
        plan_version: int,
        store_registry_version: str,
        policy_version: str,
        manifest_json: dict[str, Any],
        plan_hash: str,
        state: str,
        requester_id: str,
        expires_at: datetime,
    ) -> OnboardingDeletionManifestORM:
        """Persist an immutable manifest preview."""
        if plan_version < 1:
            raise ValueError("plan_version must be >= 1")
        async with rls_session(self._session_factory, tenant_id) as session:
            statement = (
                insert(OnboardingDeletionManifestORM)
                .values(
                    tenant_id=tenant_id,
                    manifest_id=manifest_id,
                    application_id=application_id,
                    environment=environment,
                    plan_version=plan_version,
                    store_registry_version=store_registry_version,
                    policy_version=policy_version,
                    manifest_json=manifest_json,
                    plan_hash=plan_hash,
                    state=state,
                    requester_id=requester_id,
                    expires_at=expires_at,
                )
                .on_conflict_do_nothing(constraint="uq_onboarding_manifest")
                .returning(OnboardingDeletionManifestORM.id)
            )
            row_id = await session.scalar(statement)
            if row_id is not None:
                row = await session.scalar(
                    select(OnboardingDeletionManifestORM).where(
                        OnboardingDeletionManifestORM.id == row_id
                    )
                )
            else:
                row = await session.scalar(
                    select(OnboardingDeletionManifestORM).where(
                        OnboardingDeletionManifestORM.tenant_id == tenant_id,
                        OnboardingDeletionManifestORM.manifest_id == manifest_id,
                    )
                )
            if row is None:
                raise RuntimeError("manifest write did not produce a readable row")
            if row.plan_hash != plan_hash:
                raise DeletionApprovalError("manifest already exists with a different plan hash")
            return row

    async def mint_approval(
        self,
        *,
        tenant_id: str,
        payload: ApprovalPayload,
        requester_id: str,
    ) -> tuple[OnboardingDeletionApprovalORM, str]:
        """Persist digests and return the one-time token (returned once)."""
        if payload.tenant_id != tenant_id or payload.requester_id != requester_id:
            raise DeletionApprovalError("approval payload does not match the caller scope")
        token, token_digest = mint_approval_token()
        payload_digest = canonical_approval_digest(payload)
        async with rls_session(self._session_factory, tenant_id) as session:
            manifest = await session.scalar(
                select(OnboardingDeletionManifestORM).where(
                    OnboardingDeletionManifestORM.tenant_id == tenant_id,
                    OnboardingDeletionManifestORM.manifest_id == payload.manifest_id,
                )
            )
            if manifest is None:
                raise DeletionApprovalError("manifest not found")
            if manifest.plan_hash != payload.plan_hash:
                raise DeletionApprovalError("approval plan hash does not match the manifest")
            row = OnboardingDeletionApprovalORM(
                tenant_id=tenant_id,
                manifest_id=payload.manifest_id,
                requester_id=requester_id,
                approver_id=payload.approver_id,
                manifest_hash=payload.plan_hash,
                token_digest=token_digest,
                payload_digest=payload_digest,
                policy_version=payload.policy_version,
                store_registry_version=payload.store_registry_version,
                nonce=payload.nonce,
                expires_at=payload.expires_at,
            )
            session.add(row)
            await session.flush()
            return row, token

    async def consume_approval(
        self,
        *,
        tenant_id: str,
        manifest_id: UUID,
        presented_token: str,
        approver_id: str,
        now: datetime | None = None,
    ) -> OnboardingDeletionApprovalORM:
        """Atomically consume one approval and create the purge job intent.

        Conditional single statement: only an unconsumed, unexpired,
        unrevoked row with matching digests and approver converts. Concurrent
        consumers converge on exactly one winner; everyone else fails closed.
        """
        import hashlib

        current = now or datetime.now(UTC)
        async with rls_session(self._session_factory, tenant_id) as session:
            presented_digest = hashlib.sha256(presented_token.encode("utf-8")).hexdigest()
            result = await session.execute(
                text(
                    "UPDATE onboarding_deletion_approvals SET consumed_at = clock_timestamp() "
                    "WHERE tenant_id = :tenant_id AND manifest_id = :manifest_id "
                    "AND consumed_at IS NULL AND revoked_at IS NULL "
                    "AND expires_at > clock_timestamp() "
                    "AND token_digest = :presented_digest "
                    "AND approver_id = :approver_id "
                    "RETURNING id"
                ),
                {
                    "tenant_id": tenant_id,
                    "manifest_id": manifest_id,
                    "presented_digest": presented_digest,
                    "approver_id": approver_id,
                },
            )
            approved_id = result.scalar()
            if approved_id is None:
                raise DeletionApprovalError(
                    "approval is missing, expired, revoked, already consumed, "
                    "or the token/approver does not match"
                )
            approval = await session.scalar(
                select(OnboardingDeletionApprovalORM).where(
                    OnboardingDeletionApprovalORM.id == approved_id
                )
            )
            if approval is None:  # pragma: no cover - conditional write just succeeded
                raise RuntimeError("consumed approval could not be read back")
            job_id = uuid4()
            await session.execute(
                text(
                    "INSERT INTO background_jobs (id, tenant_id, kind, contract_version, "
                    "status, progress, total, created_by, created_at, attempt, version) "
                    "VALUES (:id, :tenant_id, 'onboarding-purge', 'v1', 'QUEUED', "
                    "0, 0, 'deletion-approval', clock_timestamp(), 0, 1)"
                ),
                {"id": job_id, "tenant_id": tenant_id},
            )
            approval.purge_job_id = job_id
            await session.flush()
            if approval.expires_at <= current:
                raise DeletionApprovalError("approval expired")
            return approval

    async def record_outcome(
        self,
        *,
        tenant_id: str,
        manifest_id: UUID,
        entry_key: str,
        store_id: str,
        action: str,
        state: str,
        attempt: int,
        completion_evidence_ref: str | None = None,
        residual_code: str | None = None,
    ) -> OnboardingDeletionOutcomeORM:
        """Append one per-resource outcome (attempts are caller-sequenced)."""
        if attempt < 1:
            raise ValueError("attempt must be >= 1")
        async with rls_session(self._session_factory, tenant_id) as session:
            row = OnboardingDeletionOutcomeORM(
                tenant_id=tenant_id,
                manifest_id=manifest_id,
                entry_key=entry_key,
                store_id=store_id,
                action=action,
                state=state,
                attempt=attempt,
                completion_evidence_ref=completion_evidence_ref,
                residual_code=residual_code,
            )
            session.add(row)
            await session.flush()
            return row

    async def manifest_terminal_state(
        self, tenant_id: str, manifest_id: UUID, expected_entries: int
    ) -> str:
        """Compute the honest terminal state from recorded outcomes."""
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.execute(
                text(
                    "SELECT DISTINCT entry_key, state FROM onboarding_deletion_outcomes "
                    "WHERE tenant_id = :tenant_id AND manifest_id = :manifest_id"
                ),
                {"tenant_id": tenant_id, "manifest_id": manifest_id},
            )
            latest: dict[str, str] = {}
            for entry_key, state in result.all():
                latest[str(entry_key)] = str(state)
            if len(latest) < expected_entries:
                return DeletionStatus.INCOMPLETE.value
            return evaluate_terminal_state(list(latest.values()))

    async def update_manifest_state(self, tenant_id: str, manifest_id: UUID, state: str) -> bool:
        """Transition a manifest's lifecycle state (guarded, no outcome rewrite)."""
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.execute(
                text(
                    "UPDATE onboarding_deletion_manifests SET state = :state "
                    "WHERE tenant_id = :tenant_id AND manifest_id = :manifest_id "
                    "RETURNING id"
                ),
                {"tenant_id": tenant_id, "manifest_id": manifest_id, "state": state},
            )
            return result.first() is not None

    async def read_manifest(
        self, tenant_id: str, manifest_id: UUID
    ) -> OnboardingDeletionManifestORM | None:
        """Read one manifest, tenant-scoped."""
        async with rls_session(self._session_factory, tenant_id) as session:
            return cast(
                OnboardingDeletionManifestORM | None,
                await session.scalar(
                    select(OnboardingDeletionManifestORM).where(
                        OnboardingDeletionManifestORM.tenant_id == tenant_id,
                        OnboardingDeletionManifestORM.manifest_id == manifest_id,
                    )
                ),
            )

    async def outcome_count(self, tenant_id: str, manifest_id: UUID) -> int:
        """Count recorded outcome rows for a manifest."""
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.execute(
                text(
                    "SELECT count(*) FROM onboarding_deletion_outcomes "
                    "WHERE tenant_id = :tenant_id AND manifest_id = :manifest_id"
                ),
                {"tenant_id": tenant_id, "manifest_id": manifest_id},
            )
            return int(result.scalar() or 0)

    async def list_approvals(
        self, tenant_id: str, manifest_id: UUID
    ) -> list[OnboardingDeletionApprovalORM]:
        """All approval rows for a manifest, newest first."""
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(OnboardingDeletionApprovalORM)
                .where(
                    OnboardingDeletionApprovalORM.tenant_id == tenant_id,
                    OnboardingDeletionApprovalORM.manifest_id == manifest_id,
                )
                .order_by(OnboardingDeletionApprovalORM.created_at.desc())
            )
            return list(result.all())

    async def list_outcome_states(self, tenant_id: str, manifest_id: UUID) -> list[tuple[str, str]]:
        """Latest state per entry key for terminal evaluation."""
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.execute(
                text(
                    "SELECT DISTINCT ON (entry_key) entry_key, state "
                    "FROM onboarding_deletion_outcomes "
                    "WHERE tenant_id = :tenant_id AND manifest_id = :manifest_id "
                    "ORDER BY entry_key, attempt DESC"
                ),
                {"tenant_id": tenant_id, "manifest_id": manifest_id},
            )
            return [(str(key), str(state)) for key, state in result.all()]

    async def list_registry(
        self, deployment_id: str, registry_version: str | None = None
    ) -> list[OnboardingStoreRegistryORM]:
        """Operator registry rows for a deployment (optionally one version)."""
        async with self._session_factory() as session:
            statement = select(OnboardingStoreRegistryORM).where(
                OnboardingStoreRegistryORM.deployment_id == deployment_id
            )
            if registry_version is not None:
                statement = statement.where(
                    OnboardingStoreRegistryORM.registry_version == registry_version
                )
            result = await session.scalars(statement)
            return list(result.all())
