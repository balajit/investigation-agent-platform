"""Tenant-scoped persistence for immutable onboarding DRAFT revisions."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from investigation_agent_platform.application.onboarding.intake import TenantIntakeEnvelope
from investigation_agent_platform.infrastructure.persistence.models import OnboardingRequestORM
from investigation_agent_platform.infrastructure.persistence.rls import rls_session


class DraftIdempotencyConflictError(ValueError):
    """An idempotency key was previously bound to different intake content."""


class DraftRevisionConflictError(ValueError):
    """The requested draft revision does not match the current revision."""


class SqlAlchemyOnboardingRequestRepository:
    """Persist DRAFT revisions and CREATE audit linkage; never accept or dispatch."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def create_draft(
        self,
        *,
        tenant_id: str,
        actor_sub: str,
        idempotency_key: str,
        intake: TenantIntakeEnvelope,
        request_id: UUID | None = None,
        expected_revision: int | None = None,
        policy_version: str = "policy-v1",
        catalog_version: str = "catalog-v1",
    ) -> OnboardingRequestORM:
        """Create a DRAFT revision with its audit row in the same transaction.

        For a new request, omit ``request_id`` and ``expected_revision``. An edit
        names the request and expected latest revision; the new immutable row is
        created at the next revision. Same-key/same-hash calls replay the stored
        row, while same-key/different-hash calls fail closed. No job or dispatch
        row is written and no lifecycle transition is performed.
        """
        if not tenant_id or len(tenant_id) > 64:
            raise ValueError("tenant_id must be non-empty and at most 64 characters")
        if not actor_sub or len(actor_sub) > 256:
            raise ValueError("actor_sub must be non-empty and at most 256 characters")
        if not idempotency_key or len(idempotency_key) > 256:
            raise ValueError("idempotency_key must be non-empty and at most 256 characters")
        if (request_id is None) != (expected_revision is None):
            raise ValueError("request_id and expected_revision must be supplied together")
        if expected_revision is not None and expected_revision < 1:
            raise ValueError("expected_revision must be >= 1")

        intake_json = intake.intake.model_dump(mode="json", exclude_none=True)
        content_hash = intake.intake_content_hash
        canonical_request_id = request_id or uuid4()

        async with rls_session(self._session_factory, tenant_id) as session:
            existing = await session.scalar(
                select(OnboardingRequestORM).where(
                    OnboardingRequestORM.tenant_id == tenant_id,
                    OnboardingRequestORM.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if existing.content_hash != content_hash:
                    raise DraftIdempotencyConflictError(
                        "idempotency key is already bound to different intake content"
                    )
                return existing

            revision = 1
            if request_id is not None:
                current = await session.scalar(
                    select(OnboardingRequestORM)
                    .where(
                        OnboardingRequestORM.tenant_id == tenant_id,
                        OnboardingRequestORM.request_id == request_id,
                    )
                    .order_by(OnboardingRequestORM.revision.desc())
                    .limit(1)
                    .with_for_update()
                )
                if current is None or current.revision != expected_revision:
                    raise DraftRevisionConflictError(
                        "expected revision does not match the current draft revision"
                    )
                if current.state != "DRAFT":
                    raise DraftRevisionConflictError(
                        "only DRAFT requests can be edited in Phase 1B"
                    )
                revision = current.revision + 1

            statement = (
                insert(OnboardingRequestORM)
                .values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    request_id=canonical_request_id,
                    revision=revision,
                    idempotency_key=idempotency_key,
                    content_hash=content_hash,
                    schema_version=intake.intake.schema_version,
                    policy_version=policy_version,
                    catalog_version=catalog_version,
                    application_id=intake.intake.application_id,
                    environment=intake.intake.environment,
                    intake_json=intake_json,
                    actor_id=actor_sub,
                    job_id=None,
                    state="DRAFT",
                    state_version=1,
                )
                .on_conflict_do_nothing(constraint="uq_onboarding_request_idempotency")
                .returning(OnboardingRequestORM.id)
            )
            try:
                async with session.begin_nested():
                    inserted_id = await session.scalar(statement)
            except IntegrityError as exc:
                constraint_name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
                if constraint_name == "uq_onboarding_request_revision":
                    raise DraftRevisionConflictError(
                        "another DRAFT revision was created after the expected revision"
                    ) from exc
                raise

            if inserted_id is None:
                replay = await session.scalar(
                    select(OnboardingRequestORM).where(
                        OnboardingRequestORM.tenant_id == tenant_id,
                        OnboardingRequestORM.idempotency_key == idempotency_key,
                    )
                )
                if replay is not None and replay.content_hash != content_hash:
                    raise DraftIdempotencyConflictError(
                        "idempotency key was concurrently reserved for different intake content"
                    )
                if replay is not None:
                    return replay
                if request_id is not None:
                    raise DraftRevisionConflictError(
                        "another DRAFT revision was created after the expected revision"
                    )
                raise DraftIdempotencyConflictError("idempotency key could not be reserved")

            await session.execute(
                text(
                    "SELECT record_onboarding_request_audit("
                    ":tenant_id, :request_id, :revision, :actor_sub, 'CREATE', "
                    ":content_hash, :idempotency_key)"
                ),
                {
                    "tenant_id": tenant_id,
                    "request_id": canonical_request_id,
                    "revision": revision,
                    "actor_sub": actor_sub,
                    "content_hash": content_hash,
                    "idempotency_key": idempotency_key,
                },
            )
            row = await session.scalar(
                select(OnboardingRequestORM).where(
                    OnboardingRequestORM.tenant_id == tenant_id,
                    OnboardingRequestORM.id == inserted_id,
                )
            )
            if row is None:
                raise RuntimeError("inserted onboarding DRAFT could not be read back")
            if row.state != "DRAFT" or row.job_id is not None:
                raise RuntimeError("Phase 1B may persist only DRAFT rows without job references")
            return row
