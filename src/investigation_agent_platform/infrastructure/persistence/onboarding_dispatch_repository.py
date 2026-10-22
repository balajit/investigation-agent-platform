"""Atomic, fenced claim and reconciliation operations for onboarding dispatches."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text


@dataclass(frozen=True)
class ClaimedOnboardingDispatch:
    id: UUID
    tenant_id: str
    request_id: UUID
    revision: int
    job_id: UUID
    workflow_id: str
    schema_version: str
    execution_generation: int
    lease_owner: str
    lease_fencing_token: int
    lease_expires_at: datetime


class SqlAlchemyOnboardingDispatchRepository:
    """Fleet-wide dispatch polling with tenant-scoped request reads.

    The database connection must authenticate as ``iap_worker``. The dedicated
    RLS policy exposes only dispatch rows; request reads use ``rls_session``
    with the tenant returned by the claim.
    """

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def claim_oldest_due(
        self, *, worker_id: str, lease_duration: timedelta = timedelta(seconds=60)
    ) -> ClaimedOnboardingDispatch | None:
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must be non-empty and at most 128 characters")
        if lease_duration.total_seconds() <= 0:
            raise ValueError("lease_duration must be positive")

        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT set_config('iap.worker_id', :worker_id, true)"),
                    {"worker_id": worker_id},
                )
                result = await session.execute(
                    text(
                        """WITH candidate AS (
                               SELECT id
                               FROM onboarding_dispatches
                                WHERE state IN ('PENDING', 'RETRYABLE')
                                 AND (next_attempt_at IS NULL OR next_attempt_at <= clock_timestamp())
                                 AND (lease_expires_at IS NULL OR lease_expires_at <= clock_timestamp())
                               ORDER BY created_at, id
                               FOR UPDATE SKIP LOCKED
                               LIMIT 1
                           )
                           UPDATE onboarding_dispatches AS dispatch
                           SET state = 'CLAIMED',
                               lease_owner = :worker_id,
                               lease_fencing_token = dispatch.lease_fencing_token + 1,
                               lease_expires_at = clock_timestamp() + make_interval(secs => :lease_seconds),
                               attempts = dispatch.attempts + 1,
                               updated_at = clock_timestamp()
                           FROM candidate
                           WHERE dispatch.id = candidate.id
                           RETURNING dispatch.id, dispatch.tenant_id, dispatch.request_id,
                                     dispatch.revision, dispatch.job_id, dispatch.workflow_id,
                                     dispatch.schema_version, dispatch.execution_generation,
                                     dispatch.lease_owner, dispatch.lease_fencing_token,
                                     dispatch.lease_expires_at"""
                    ),
                    {
                        "worker_id": worker_id,
                        "lease_seconds": lease_duration.total_seconds(),
                    },
                )
                row = result.mappings().first()
                if row is None:
                    return None
                claim = ClaimedOnboardingDispatch(**dict(row))
                await self._set_tenant_context(session, claim.tenant_id)
                await session.execute(
                    text("SELECT set_config('iap.worker_id', :worker_id, true)"),
                    {"worker_id": worker_id},
                )
                await session.execute(
                    text(
                        "SELECT record_onboarding_dispatch_audit(:tenant_id, :dispatch_id, "
                        "'CLAIM', :fencing_token)"
                    ),
                    {
                        "tenant_id": claim.tenant_id,
                        "dispatch_id": claim.id,
                        "fencing_token": claim.lease_fencing_token,
                    },
                )
            return claim

    async def requeue_one_expired_lease(self, *, worker_id: str) -> UUID | None:
        """Fence one expired claim and return it to RETRYABLE before polling."""
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must be non-empty and at most 128 characters")
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text("SELECT set_config('iap.worker_id', :worker_id, true)"),
                    {"worker_id": worker_id},
                )
                result = await session.execute(
                    text(
                        """WITH candidate AS (
                               SELECT id
                               FROM onboarding_dispatches
                               WHERE state = 'CLAIMED'
                                 AND lease_expires_at <= clock_timestamp()
                               ORDER BY lease_expires_at, id
                               FOR UPDATE SKIP LOCKED
                               LIMIT 1
                           )
                           UPDATE onboarding_dispatches AS dispatch
                           SET state = 'RETRYABLE', lease_owner = NULL,
                               lease_expires_at = NULL, updated_at = clock_timestamp()
                           FROM candidate
                           WHERE dispatch.id = candidate.id
                           RETURNING dispatch.id, dispatch.tenant_id,
                                     dispatch.lease_fencing_token"""
                    )
                )
                row = result.mappings().first()
                if row is None:
                    return None
                await self._set_tenant_context(session, row["tenant_id"])
                await session.execute(
                    text("SELECT set_config('iap.worker_id', :worker_id, true)"),
                    {"worker_id": worker_id},
                )
                await session.execute(
                    text(
                        "SELECT record_onboarding_dispatch_audit(:tenant_id, :dispatch_id, "
                        "'STALE_FENCE', :fencing_token)"
                    ),
                    {
                        "tenant_id": row["tenant_id"],
                        "dispatch_id": row["id"],
                        "fencing_token": row["lease_fencing_token"],
                    },
                )
                dispatch_id = row["id"]
                if not isinstance(dispatch_id, UUID):
                    dispatch_id = UUID(str(dispatch_id))
                return dispatch_id

    async def renew_lease(
        self,
        claim: ClaimedOnboardingDispatch,
        *,
        worker_id: str,
        lease_duration: timedelta = timedelta(seconds=60),
    ) -> datetime | None:
        return await self._change_lease(
            claim,
            worker_id=worker_id,
            action="RENEW",
            lease_duration=lease_duration,
        )

    async def release_lease(self, claim: ClaimedOnboardingDispatch, *, worker_id: str) -> bool:
        changed = await self._change_lease(
            claim, worker_id=worker_id, action="RELEASE", lease_duration=None
        )
        return changed is not None

    async def record_stale_fence(self, claim: ClaimedOnboardingDispatch) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await self._set_tenant_context(session, claim.tenant_id)
                await session.execute(
                    text("SELECT set_config('iap.worker_id', :worker_id, true)"),
                    {"worker_id": claim.lease_owner},
                )
                await session.execute(
                    text(
                        "SELECT record_onboarding_dispatch_audit(:tenant_id, :dispatch_id, "
                        "'STALE_FENCE', :fencing_token)"
                    ),
                    {
                        "tenant_id": claim.tenant_id,
                        "dispatch_id": claim.id,
                        "fencing_token": claim.lease_fencing_token,
                    },
                )

    async def claim_and_reconcile(
        self,
        *,
        worker_id: str,
        lease_duration: timedelta = timedelta(seconds=60),
    ) -> ClaimedOnboardingDispatch | None:
        """Fence one expired lease before claiming the next due dispatch."""
        await self.requeue_one_expired_lease(worker_id=worker_id)
        return await self.claim_oldest_due(worker_id=worker_id, lease_duration=lease_duration)

    async def read_request(
        self, claim: ClaimedOnboardingDispatch, *, request_id: UUID | None = None
    ) -> dict[str, Any] | None:
        """Read request/intake only after setting tenant-local RLS context."""
        from investigation_agent_platform.infrastructure.persistence.rls import rls_session

        target_request_id = request_id or claim.request_id
        async with rls_session(self._session_factory, claim.tenant_id) as session:
            await session.execute(
                text("SELECT set_config('iap.worker_id', :worker_id, true)"),
                {"worker_id": claim.lease_owner},
            )
            result = await session.execute(
                text(
                    "SELECT tenant_id, request_id, revision, schema_version, policy_version, "
                    "catalog_version, application_id, environment, intake_json, actor_id, state "
                    "FROM onboarding_requests "
                    "WHERE tenant_id = :tenant_id AND request_id = :request_id "
                    "AND revision = :revision"
                ),
                {
                    "tenant_id": claim.tenant_id,
                    "request_id": target_request_id,
                    "revision": claim.revision,
                },
            )
            row = result.mappings().first()
            return dict(row) if row is not None else None

    async def _change_lease(
        self,
        claim: ClaimedOnboardingDispatch,
        *,
        worker_id: str,
        action: str,
        lease_duration: timedelta | None,
    ) -> datetime | None:
        if worker_id != claim.lease_owner:
            await self.record_stale_fence(claim)
            return None
        async with self._session_factory() as session:
            async with session.begin():
                await self._set_tenant_context(session, claim.tenant_id)
                await session.execute(
                    text("SELECT set_config('iap.worker_id', :worker_id, true)"),
                    {"worker_id": claim.lease_owner},
                )
                if action == "RENEW":
                    if lease_duration is None or lease_duration.total_seconds() <= 0:
                        raise ValueError("lease_duration must be positive")
                    result = await session.execute(
                        text(
                            "UPDATE onboarding_dispatches SET lease_expires_at = "
                            "clock_timestamp() + make_interval(secs => :lease_seconds), "
                            "updated_at = clock_timestamp() "
                            "WHERE tenant_id = :tenant_id AND id = :dispatch_id "
                            "AND state = 'CLAIMED' AND lease_owner = :worker_id "
                            "AND lease_fencing_token = :fencing_token "
                            "AND lease_expires_at > clock_timestamp() "
                            "RETURNING lease_expires_at"
                        ),
                        {
                            "lease_seconds": lease_duration.total_seconds(),
                            "tenant_id": claim.tenant_id,
                            "dispatch_id": claim.id,
                            "worker_id": worker_id,
                            "fencing_token": claim.lease_fencing_token,
                        },
                    )
                    row = result.mappings().first()
                    if row is None:
                        await session.execute(
                            text(
                                "SELECT record_onboarding_dispatch_audit(:tenant_id, :dispatch_id, "
                                "'STALE_FENCE', :fencing_token)"
                            ),
                            {
                                "tenant_id": claim.tenant_id,
                                "dispatch_id": claim.id,
                                "fencing_token": claim.lease_fencing_token,
                            },
                        )
                        await session.flush()
                        return None
                    await session.execute(
                        text(
                            "SELECT record_onboarding_dispatch_audit(:tenant_id, :dispatch_id, "
                            "'RENEW', :fencing_token)"
                        ),
                        {
                            "tenant_id": claim.tenant_id,
                            "dispatch_id": claim.id,
                            "fencing_token": claim.lease_fencing_token,
                        },
                    )
                    await session.flush()
                    expires_at = row["lease_expires_at"]
                    if not isinstance(expires_at, datetime):
                        raise TypeError("database returned invalid lease expiry")
                    return expires_at

                result = await session.execute(
                    text(
                        "UPDATE onboarding_dispatches SET state = 'RETRYABLE', lease_owner = NULL, "
                        "lease_expires_at = NULL, updated_at = clock_timestamp() "
                        "WHERE tenant_id = :tenant_id AND id = :dispatch_id "
                        "AND state = 'CLAIMED' AND lease_owner = :worker_id "
                        "AND lease_fencing_token = :fencing_token "
                        "AND lease_expires_at > clock_timestamp() RETURNING id"
                    ),
                    {
                        "tenant_id": claim.tenant_id,
                        "dispatch_id": claim.id,
                        "worker_id": worker_id,
                        "fencing_token": claim.lease_fencing_token,
                    },
                )
                if result.first() is None:
                    await session.execute(
                        text(
                            "SELECT record_onboarding_dispatch_audit(:tenant_id, :dispatch_id, "
                            "'STALE_FENCE', :fencing_token)"
                        ),
                        {
                            "tenant_id": claim.tenant_id,
                            "dispatch_id": claim.id,
                            "fencing_token": claim.lease_fencing_token,
                        },
                    )
                    await session.flush()
                    return None
                await session.execute(
                    text(
                        "SELECT record_onboarding_dispatch_audit(:tenant_id, :dispatch_id, "
                        "'RELEASE', :fencing_token)"
                    ),
                    {
                        "tenant_id": claim.tenant_id,
                        "dispatch_id": claim.id,
                        "fencing_token": claim.lease_fencing_token,
                    },
                )
                await session.flush()
                return datetime.now(UTC)

    @staticmethod
    async def _set_tenant_context(session: Any, tenant_id: str) -> None:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": tenant_id},
        )
