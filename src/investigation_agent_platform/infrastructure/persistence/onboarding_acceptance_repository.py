# src/investigation_agent_platform/infrastructure/persistence/onboarding_acceptance_repository.py
"""Atomic DRAFT→QUEUED acceptance for tenant onboarding (Phase 2).

One transaction per acceptance: CAS the request revision to QUEUED, insert the
background job row, insert the dispatch (outbox intent) row, and file the
ACCEPT audit record. Anything less than all four commits nothing. No routes,
no workflows, and no state beyond QUEUED are touched here — dispatch
execution belongs to the dispatcher worker, activation to later phases.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from investigation_agent_platform.infrastructure.persistence.rls import (
    rls_session,
)

DISPATCH_SCHEMA_VERSION = "onboarding-dispatch/v1"
ACCEPT_JOB_KIND = "onboarding-acceptance"
ACCEPT_JOB_CONTRACT_VERSION = "v1"
TERMINAL_DISPATCH_STATES = ("TERMINAL",)


class AcceptanceConflictError(ValueError):
    """A conflicting dispatch already exists for these coordinates."""


class AcceptanceStaleError(ValueError):
    """The request revision is missing, not DRAFT, or already superseded."""


class AcceptanceGenerationError(ValueError):
    """The requested execution generation is invalid for the current state."""


@dataclass(frozen=True)
class AcceptedOnboardingDispatch:
    id: UUID
    tenant_id: str
    request_id: UUID
    revision: int
    execution_generation: int
    job_id: UUID
    workflow_id: str
    state: str


def deterministic_workflow_id(
    tenant_id: str, request_id: UUID, revision: int, generation: int
) -> str:
    """Stable workflow identity: redelivery reuses it, new business recovery
    bumps the generation and therefore the identity. Never embeds evidence."""
    if revision < 1 or generation < 1:
        raise ValueError("revision and generation must be >= 1")
    return f"onboarding/{tenant_id}/{request_id}/r{revision}g{generation}"


class SqlAlchemyOnboardingAcceptanceRepository:
    """Accept DRAFT revisions into QUEUED dispatches, atomically and idempotently."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def accept_draft(
        self,
        *,
        tenant_id: str,
        actor_sub: str,
        request_id: UUID,
        expected_revision: int,
        execution_generation: int | None = None,
        expected_state_version: int | None = None,
    ) -> AcceptedOnboardingDispatch:
        """Accept one immutable revision. Idempotent on repeat calls.

        ``execution_generation`` omitted means "accept (or return the existing
        acceptance)". An explicit generation N returns the existing row when
        present, or opens generation N when it is exactly one past the latest
        TERMINAL generation. Anything else (skips, non-terminal predecessors,
        canceled requests) fails closed.
        """
        if not tenant_id or len(tenant_id) > 64:
            raise ValueError("tenant_id must be non-empty and at most 64 characters")
        if not actor_sub or len(actor_sub) > 256:
            raise ValueError("actor_sub must be non-empty and at most 256 characters")
        if expected_revision < 1:
            raise ValueError("expected_revision must be >= 1")
        if execution_generation is not None and execution_generation < 1:
            raise ValueError("execution_generation must be >= 1 when supplied")
        if expected_state_version is not None and expected_state_version < 1:
            raise ValueError("expected_state_version must be >= 1 when supplied")

        coords = {
            "tenant_id": tenant_id,
            "request_id": request_id,
            "revision": expected_revision,
        }
        async with rls_session(self._session_factory, tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT id, tenant_id, request_id, revision, "
                            "idempotency_key, content_hash, schema_version, "
                            "policy_version, catalog_version, state, state_version "
                            "FROM onboarding_requests WHERE tenant_id = :tenant_id "
                            "AND request_id = :request_id AND revision = :revision "
                            "FOR UPDATE"
                        ),
                        coords,
                    )
                )
                .mappings()
                .all()
            )
            if not rows:
                raise AcceptanceStaleError("no such onboarding request revision to accept")
            row = rows[0]
            if row["state"] in ("CANCEL_REQUESTED", "CANCELED"):
                raise AcceptanceStaleError("canceled requests cannot be accepted")

            existing = (
                (
                    await session.execute(
                        text(
                            "SELECT id, tenant_id, request_id, revision, "
                            "execution_generation, job_id, workflow_id, state "
                            "FROM onboarding_dispatches WHERE tenant_id = :tenant_id "
                            "AND request_id = :request_id AND revision = :revision "
                            "ORDER BY execution_generation DESC"
                        ),
                        coords,
                    )
                )
                .mappings()
                .all()
            )

            if execution_generation is None:
                if existing:
                    return self._accepted_from(existing[0])
                generation = 1
            else:
                match = next(
                    (
                        existing_row
                        for existing_row in existing
                        if existing_row["execution_generation"] == execution_generation
                    ),
                    None,
                )
                if match is not None:
                    return self._accepted_from(match)
                latest = max(
                    (existing_row["execution_generation"] for existing_row in existing),
                    default=0,
                )
                if execution_generation != latest + 1:
                    raise AcceptanceGenerationError(
                        "execution generations must advance by exactly one"
                    )
                if latest > 0:
                    latest_row = next(
                        existing_row
                        for existing_row in existing
                        if existing_row["execution_generation"] == latest
                    )
                    if latest_row["state"] not in TERMINAL_DISPATCH_STATES:
                        raise AcceptanceGenerationError(
                            "a new execution generation requires the latest dispatch to be terminal"
                        )
                generation = execution_generation

            if generation == 1:
                if row["state"] != "DRAFT":
                    raise AcceptanceStaleError(
                        "only DRAFT revisions can be accepted as generation 1"
                    )
            elif row["state"] != "QUEUED":
                raise AcceptanceStaleError(
                    "follow-on generations require the request to stay QUEUED"
                )

            workflow_id = deterministic_workflow_id(
                tenant_id, request_id, expected_revision, generation
            )
            job_id = uuid4()
            content_hash: str = row["content_hash"]
            idempotency_key: str = row["idempotency_key"]

            # The job row must exist before the CAS references it
            # (fk_onboarding_request_tenant_job). It is removed again on any
            # failure path below, so failed accepts leave no orphans.
            await session.execute(
                text(
                    "INSERT INTO background_jobs (id, tenant_id, kind, "
                    "contract_version, status, created_by, workflow_id, "
                    "progress, total, created_at, attempt, version) VALUES "
                    "(:id, :tenant_id, :kind, :contract_version, 'PENDING', "
                    ":created_by, :workflow_id, 0, 0, clock_timestamp(), 0, 1)"
                ),
                {
                    "id": job_id,
                    "tenant_id": tenant_id,
                    "kind": ACCEPT_JOB_KIND,
                    "contract_version": ACCEPT_JOB_CONTRACT_VERSION,
                    "created_by": actor_sub,
                    "workflow_id": workflow_id,
                },
            )

            if generation == 1:
                cas_params: dict[str, Any] = dict(coords, job_id=job_id)
                cas_where = "AND revision = :revision AND state = 'DRAFT'"
                if expected_state_version is not None:
                    cas_where += " AND state_version = :expected_state_version"
                    cas_params["expected_state_version"] = expected_state_version
                cas = (
                    await session.execute(
                        text(
                            "UPDATE onboarding_requests SET state = 'QUEUED', "
                            "state_version = state_version + 1, job_id = :job_id "
                            "WHERE tenant_id = :tenant_id "
                            "AND request_id = :request_id "
                            f"{cas_where} RETURNING id"
                        ),
                        cas_params,
                    )
                ).scalar_one_or_none()
                if cas is None:
                    # Lost a race with a concurrent accept: the winner holds
                    # the row lock, so re-reading sees the committed dispatch.
                    winner = await self._read_dispatch(session, coords, generation)
                    if winner is not None:
                        await session.execute(
                            text("DELETE FROM background_jobs WHERE id = :job_id"),
                            {"job_id": job_id},
                        )
                        return self._accepted_from(winner)
                    await session.execute(
                        text("DELETE FROM background_jobs WHERE id = :job_id"),
                        {"job_id": job_id},
                    )
                    raise AcceptanceStaleError(
                        "request revision changed before acceptance could land"
                    )

            inserted_id = (
                await session.execute(
                    text(
                        "INSERT INTO onboarding_dispatches (id, tenant_id, "
                        "event_id, request_id, revision, job_id, workflow_id, "
                        "schema_version, state, execution_generation) VALUES "
                        "(:id, :tenant_id, :event_id, :request_id, :revision, "
                        ":job_id, :workflow_id, :schema_version, 'PENDING', "
                        ":generation) ON CONFLICT DO NOTHING RETURNING id"
                    ),
                    {
                        "id": uuid4(),
                        "tenant_id": tenant_id,
                        "event_id": uuid4(),
                        "request_id": request_id,
                        "revision": expected_revision,
                        "job_id": job_id,
                        "workflow_id": workflow_id,
                        "schema_version": DISPATCH_SCHEMA_VERSION,
                        "generation": generation,
                    },
                )
            ).scalar_one_or_none()
            if inserted_id is None:
                # Lost the insert race: drop our orphaned job row (nothing
                # references it — our dispatch never landed), then return
                # the winner's row so the retry is idempotent.
                await session.execute(
                    text("DELETE FROM background_jobs WHERE id = :job_id"),
                    {"job_id": job_id},
                )
                winner = await self._read_dispatch(session, coords, generation)
                if winner is not None:
                    return self._accepted_from(winner)
                raise AcceptanceConflictError(
                    "a conflicting dispatch already exists for these coordinates"
                )

            await session.execute(
                text(
                    "SELECT record_onboarding_request_audit(:tenant_id, "
                    ":request_id, :revision, :actor_sub, 'ACCEPT', "
                    ":content_hash, :idempotency_key)"
                ),
                {
                    "tenant_id": tenant_id,
                    "request_id": request_id,
                    "revision": expected_revision,
                    "actor_sub": actor_sub,
                    "content_hash": content_hash,
                    "idempotency_key": idempotency_key,
                },
            )

            stored = await self._read_dispatch(session, coords, generation)
            if stored is None:
                raise RuntimeError("accepted dispatch could not be read back")
            if stored["state"] != "PENDING" or stored["job_id"] != job_id:
                raise RuntimeError("accepted dispatch failed post-write verification")
            return self._accepted_from(stored)

    @staticmethod
    async def _read_dispatch(session: Any, coords: dict[str, Any], generation: int) -> Any | None:
        rows = (
            await session.execute(
                text(
                    "SELECT id, tenant_id, request_id, revision, "
                    "execution_generation, job_id, workflow_id, state "
                    "FROM onboarding_dispatches WHERE tenant_id = :tenant_id "
                    "AND request_id = :request_id AND revision = :revision "
                    "AND execution_generation = :generation"
                ),
                {**coords, "generation": generation},
            )
        ).mappings()
        return rows.first()

    @staticmethod
    def _accepted_from(row: Any) -> AcceptedOnboardingDispatch:
        def as_uuid(value: Any) -> UUID:
            return value if isinstance(value, UUID) else UUID(str(value))

        return AcceptedOnboardingDispatch(
            id=as_uuid(row["id"]),
            tenant_id=row["tenant_id"],
            request_id=as_uuid(row["request_id"]),
            revision=row["revision"],
            execution_generation=row["execution_generation"],
            job_id=as_uuid(row["job_id"]),
            workflow_id=row["workflow_id"],
            state=row["state"],
        )

    async def record_activation_audit(
        self,
        *,
        tenant_id: str,
        actor_sub: str,
        request_id: UUID,
        revision: int,
        content_hash: str,
        idempotency_key: str,
    ) -> None:
        """File the append-only ACTIVATE audit row for an approved revision.

        Called after the registry transition commits; audit and activation
        are separate durable facts, linked by coordinates.
        """
        if not tenant_id or len(tenant_id) > 64:
            raise ValueError("tenant_id must be non-empty and at most 64 characters")
        if not actor_sub or len(actor_sub) > 256:
            raise ValueError("actor_sub must be non-empty and at most 256 characters")
        if revision < 1:
            raise ValueError("revision must be >= 1")
        async with rls_session(self._session_factory, tenant_id) as session:
            await session.execute(
                text(
                    "SELECT record_onboarding_request_audit(:tenant_id, "
                    ":request_id, :revision, :actor_sub, 'ACTIVATE', "
                    ":content_hash, :idempotency_key)"
                ),
                {
                    "tenant_id": tenant_id,
                    "request_id": request_id,
                    "revision": revision,
                    "actor_sub": actor_sub,
                    "content_hash": content_hash,
                    "idempotency_key": idempotency_key,
                },
            )
