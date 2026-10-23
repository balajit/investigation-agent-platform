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

    async def read_request_row(
        self, tenant_id: str, request_id: Any, revision: int
    ) -> dict[str, Any] | None:
        """Read one intake revision row, tenant-scoped, including content hash.

        The claim-bound ``read_request`` below covers fleet-claimed reads;
        this variant serves tenant-scoped callers (activities, guards) that
        hold coordinates directly.
        """
        from investigation_agent_platform.infrastructure.persistence.rls import (
            rls_session,
        )

        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.execute(
                text(
                    "SELECT tenant_id, request_id, revision, idempotency_key, "
                    "content_hash, schema_version, policy_version, "
                    "catalog_version, application_id, environment, intake_json, "
                    "actor_id, state, state_version, job_id "
                    "FROM onboarding_requests "
                    "WHERE tenant_id = :tenant_id AND request_id = :request_id "
                    "AND revision = :revision"
                ),
                {
                    "tenant_id": tenant_id,
                    "request_id": request_id,
                    "revision": revision,
                },
            )
            row = result.mappings().first()
            return dict(row) if row is not None else None

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

    async def read_dispatch(
        self, tenant_id: str, dispatch_id: Any
    ) -> dict[str, Any] | None:
        """Read one dispatch row, tenant-scoped. No state change."""
        async with self._session_factory() as session:
            await self._set_tenant_context(session, tenant_id)
            result = await session.execute(
                text(
                    "SELECT id, tenant_id, request_id, revision, job_id, "
                    "workflow_id, schema_version, state, attempts, "
                    "next_attempt_at, lease_owner, lease_fencing_token, "
                    "lease_expires_at, execution_generation, temporal_run_id, "
                    "last_error_code, created_at, updated_at "
                    "FROM onboarding_dispatches "
                    "WHERE tenant_id = :tenant_id AND id = :dispatch_id"
                ),
                {"tenant_id": tenant_id, "dispatch_id": dispatch_id},
            )
            row = result.mappings().first()
            return dict(row) if row is not None else None

    async def record_workflow_accepted(
        self, tenant_id: str, dispatch_id: Any, fencing_token: int, run_id: str
    ) -> bool:
        """Record a started run and release the claim. CAS on fencing token.

        No worker identity is required: the RLS WITH CHECK passes because
        this write clears the lease owner to NULL. No dispatch audit row:
        the sealed 024 contract audits lease events only; acceptance is
        evidenced by the CLAIM audit (same fencing token) plus the recorded
        run id. A superseded holder writes nothing.
        """
        if not run_id:
            raise ValueError("run_id must be non-empty")
        async with self._session_factory() as session:
            async with session.begin():
                await self._set_tenant_context(session, tenant_id)
                result = await session.execute(
                    text(
                        "UPDATE onboarding_dispatches SET state = 'ACCEPTED', "
                        "temporal_run_id = :run_id, lease_owner = NULL, "
                        "lease_expires_at = NULL, updated_at = clock_timestamp() "
                        "WHERE tenant_id = :tenant_id AND id = :dispatch_id "
                        "AND state = 'CLAIMED' "
                        "AND lease_fencing_token = :fencing_token "
                        "RETURNING id"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "dispatch_id": dispatch_id,
                        "fencing_token": fencing_token,
                        "run_id": run_id,
                    },
                )
                return result.first() is not None

    async def release_for_retry(
        self,
        tenant_id: str,
        dispatch_id: Any,
        worker_id: str,
        fencing_token: int,
        error_code: str,
        backoff_seconds: float,
    ) -> bool:
        """Release a held claim back to RETRYABLE with a backoff and audit.

        Stale holders are refused (CAS on the fencing token); the reconciler
        owns recovery from there.
        """
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must be non-empty and at most 128 characters")
        if backoff_seconds < 0:
            raise ValueError("backoff_seconds must be non-negative")
        async with self._session_factory() as session:
            async with session.begin():
                await self._set_tenant_context(session, tenant_id)
                await session.execute(
                    text("SELECT set_config('iap.worker_id', :worker_id, true)"),
                    {"worker_id": worker_id},
                )
                result = await session.execute(
                    text(
                        "UPDATE onboarding_dispatches SET state = 'RETRYABLE', "
                        "lease_owner = NULL, lease_expires_at = NULL, "
                        "last_error_code = :error_code, "
                        "next_attempt_at = clock_timestamp() + "
                        "make_interval(secs => :backoff_seconds), "
                        "updated_at = clock_timestamp() "
                        "WHERE tenant_id = :tenant_id AND id = :dispatch_id "
                        "AND state = 'CLAIMED' "
                        "AND lease_fencing_token = :fencing_token "
                        "RETURNING id"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "dispatch_id": dispatch_id,
                        "fencing_token": fencing_token,
                        "error_code": error_code[:64],
                        "backoff_seconds": backoff_seconds,
                    },
                )
                if result.first() is None:
                    return False
                await session.execute(
                    text(
                        "SELECT record_onboarding_dispatch_audit(:tenant_id, :dispatch_id, "
                        "'RELEASE', :fencing_token)"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "dispatch_id": dispatch_id,
                        "fencing_token": fencing_token,
                    },
                )
                await session.flush()
                return True

    async def mark_terminal(
        self,
        tenant_id: str,
        dispatch_id: Any,
        error_code: str,
        fencing_token: int | None = None,
        only_from: tuple[str, ...] | None = None,
    ) -> bool:
        """Mark a dispatch terminally failed. No audit row: the sealed 024
        contract audits lease events only; terminal attribution lives on the
        row (error code, fencing generation) plus the CLAIM audit chain."""
        if not error_code:
            raise ValueError("error_code must be non-empty")
        states = (
            "AND state IN ("
            + ", ".join(f"'{state}'" for state in (only_from or ()))
            + ")"
            if only_from
            else ""
        )
        params: dict[str, Any] = {
            "tenant_id": tenant_id,
            "dispatch_id": dispatch_id,
            "error_code": error_code[:64],
        }
        token_clause = ""
        if fencing_token is not None:
            token_clause = " AND lease_fencing_token = :fencing_token"
            params["fencing_token"] = fencing_token
        async with self._session_factory() as session:
            async with session.begin():
                await self._set_tenant_context(session, tenant_id)
                result = await session.execute(
                    text(
                        "UPDATE onboarding_dispatches SET state = 'TERMINAL', "
                        "lease_owner = NULL, lease_expires_at = NULL, "
                        "last_error_code = :error_code, "
                        "updated_at = clock_timestamp() "
                        "WHERE tenant_id = :tenant_id AND id = :dispatch_id "
                        f"{states}{token_clause} RETURNING id"
                    ),
                    params,
                )
                return result.first() is not None

    async def list_by_state(
        self, state: str, limit: int = 25
    ) -> list[dict[str, Any]]:
        """Fleet-wide scan of dispatches in one state (reconciler use).

        No tenant context is set: the runtime dispatch policy is fleet-scoped
        by design (single cross-tenant policy from Phase 0R).
        """
        if limit < 1:
            raise ValueError("limit must be >= 1")
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT id, tenant_id, request_id, revision, job_id, "
                    "workflow_id, schema_version, state, attempts, "
                    "next_attempt_at, lease_owner, lease_fencing_token, "
                    "lease_expires_at, execution_generation, temporal_run_id, "
                    "last_error_code, created_at, updated_at "
                    "FROM onboarding_dispatches WHERE state = :state "
                    "ORDER BY updated_at, id LIMIT :limit"
                ),
                {"state": state, "limit": limit},
            )
            return [dict(row) for row in result.mappings().all()]

    async def adopt_run_id(
        self, tenant_id: str, dispatch_id: Any, run_id: str
    ) -> bool:
        """Adopt a reconciled run id for an ACCEPTED dispatch.

        Projection-drift repair only: the reconciler verified via describe
        that a different run id is the live one. CAS on ACCEPTED so a
        concurrently transitioned dispatch is never overwritten.
        """
        if not run_id:
            raise ValueError("run_id must be non-empty")
        async with self._session_factory() as session:
            async with session.begin():
                await self._set_tenant_context(session, tenant_id)
                result = await session.execute(
                    text(
                        "UPDATE onboarding_dispatches SET temporal_run_id = :run_id, "
                        "updated_at = clock_timestamp() "
                        "WHERE tenant_id = :tenant_id AND id = :dispatch_id "
                        "AND state = 'ACCEPTED' RETURNING id"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "dispatch_id": dispatch_id,
                        "run_id": run_id,
                    },
                )
                return result.first() is not None

    async def read_request_state(
        self, tenant_id: str, request_id: Any, revision: int
    ) -> str | None:
        """Tenant-scoped request state read for dispatcher guards."""
        async with self._session_factory() as session:
            await self._set_tenant_context(session, tenant_id)
            result = await session.execute(
                text(
                    "SELECT state FROM onboarding_requests "
                    "WHERE tenant_id = :tenant_id AND request_id = :request_id "
                    "AND revision = :revision"
                ),
                {
                    "tenant_id": tenant_id,
                    "request_id": request_id,
                    "revision": revision,
                },
            )
            row = result.mappings().first()
            return row["state"] if row is not None else None

    async def complete_request_cancellation(
        self, tenant_id: str, request_id: Any, revision: int
    ) -> bool:
        """Move QUEUED/CANCEL_REQUESTED to CANCELED. Idempotent on repeats.

        Activation states are out of scope: only cancellation moves live here;
        anything else is a plan decision, not an engineering default.
        """
        async with self._session_factory() as session:
            async with session.begin():
                await self._set_tenant_context(session, tenant_id)
                result = await session.execute(
                    text(
                        "UPDATE onboarding_requests SET state = 'CANCELED', "
                        "state_version = state_version + 1, "
                        "updated_at = clock_timestamp() "
                        "WHERE tenant_id = :tenant_id AND request_id = :request_id "
                        "AND revision = :revision "
                        "AND state IN ('QUEUED', 'CANCEL_REQUESTED') "
                        "RETURNING id"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "request_id": request_id,
                        "revision": revision,
                    },
                )
                return result.first() is not None
