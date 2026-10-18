# src/investigation_agent_platform/infrastructure/persistence/profile_repository.py
"""SQLAlchemy application profile repository (F-033/F-034: tenant-aware contract, versioned).

Part 11.2: profiles are immutable revisions keyed by ``(tenant_id, id,
version)``. ``save()`` never overwrites an existing revision row — a
resubmission of the same ``(tenant_id, id, version)`` is a no-op (idempotent
retry), and a genuinely new revision must carry a higher ``version``.
``get_by_application_id`` with ``version=None`` resolves the latest revision;
with an explicit version it resolves exactly that revision or ``None`` —
never silently falling back to latest.
"""

import logging
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from investigation_agent_platform.api.errors import sanitize_extra
from investigation_agent_platform.domain.profile.models import ApplicationProfile
from investigation_agent_platform.infrastructure.persistence.models import ApplicationProfileORM
from investigation_agent_platform.infrastructure.persistence.rls import rls_session

logger = logging.getLogger(__name__)


class SqlAlchemyApplicationProfileRepository:
    """PostgreSQL-backed ApplicationProfileRepository over the application_profiles table."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    @staticmethod
    def _to_orm(profile: ApplicationProfile, tenant_id: str) -> ApplicationProfileORM:
        from uuid import uuid4 as _uuid4

        return ApplicationProfileORM(
            row_id=_uuid4(),
            id=profile.id,
            tenant_id=tenant_id,
            version=profile.version,
            name=profile.name,
            description=profile.description,
            environment=profile.environment,
            status=profile.status.value,
            domain=profile.domain,
            planes=list(profile.planes),
            profile_json={
                **profile.model_dump(mode="json", by_alias=True),
                "version": profile.version,
            },
        )

    @staticmethod
    def _from_orm(row: ApplicationProfileORM) -> ApplicationProfile:
        return ApplicationProfile.model_validate(row.profile_json)

    async def get_by_application_id(
        self, tenant_id: str, application_id: str, version: str | int | None = None
    ) -> ApplicationProfile | None:
        async with rls_session(self._session_factory, tenant_id) as session:
            filters = [
                ApplicationProfileORM.id == application_id,
                ApplicationProfileORM.tenant_id == tenant_id,
            ]
            if version is not None:
                try:
                    filters.append(ApplicationProfileORM.version == int(version))
                except (TypeError, ValueError):
                    return None
            result = await session.scalars(
                select(ApplicationProfileORM)
                .where(*filters)
                .order_by(ApplicationProfileORM.version.desc())
                .limit(1)
            )
            row = result.first()
            return self._from_orm(row) if row is not None else None

    async def list_revisions(self, tenant_id: str, application_id: str) -> list[ApplicationProfile]:
        """All historical revisions for one application id, newest first."""
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(ApplicationProfileORM)
                .where(
                    ApplicationProfileORM.tenant_id == tenant_id,
                    ApplicationProfileORM.id == application_id,
                )
                .order_by(ApplicationProfileORM.version.desc())
            )
            return [self._from_orm(row) for row in result.all()]

    async def list(
        self, tenant_id: str, status: str | None = None
    ) -> list[ApplicationProfile]:
        """Latest revision per application id, tenant-scoped, optional status filter."""
        async with rls_session(self._session_factory, tenant_id) as session:
            filters = [ApplicationProfileORM.tenant_id == tenant_id]
            if status is not None:
                filters.append(ApplicationProfileORM.status == status)
            result = await session.scalars(
                select(ApplicationProfileORM)
                .where(*filters)
                .order_by(ApplicationProfileORM.id, ApplicationProfileORM.version.desc())
            )
            latest: dict[str, ApplicationProfileORM] = {}
            for row in result.all():
                if row.id not in latest:
                    latest[row.id] = row
            return [self._from_orm(row) for row in latest.values()]

    async def save(self, tenant_id: str, profile: ApplicationProfile) -> None:
        """Insert one immutable revision. Idempotent on (tenant_id, id, version)."""
        orm = self._to_orm(profile, tenant_id)
        try:
            async with rls_session(self._session_factory, tenant_id) as session:
                stmt = (
                    insert(ApplicationProfileORM)
                    .values(
                        row_id=orm.row_id,
                        id=orm.id,
                        tenant_id=tenant_id,
                        version=orm.version,
                        name=orm.name,
                        description=orm.description,
                        environment=orm.environment,
                        status=orm.status,
                        domain=orm.domain,
                        planes=orm.planes,
                        profile_json=orm.profile_json,
                    )
                    .on_conflict_do_nothing(constraint="uq_app_profile_tenant_id_version")
                )
                await session.execute(stmt)
        except Exception as exc:
            logger.exception(
                "profile save failed",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "event": "profile.save",
                        "outcome": "failed",
                        "application_id": profile.id,
                        "version": profile.version,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                ),
            )
            raise
        logger.info(
            "profile saved",
            extra=sanitize_extra(
                {
                    "tenant_id": tenant_id,
                    "event": "profile.save",
                    "outcome": "inserted",
                    "application_id": profile.id,
                    "version": profile.version,
                }
            ),
        )

    async def delete(
        self, tenant_id: str, application_id: str, version: str | int | None = None
    ) -> int:
        """Delete one revision or a whole application id. Returns rows removed."""
        try:
            async with rls_session(self._session_factory, tenant_id) as session:
                stmt = (
                    delete(ApplicationProfileORM)
                    .where(ApplicationProfileORM.tenant_id == tenant_id)
                    .where(ApplicationProfileORM.id == application_id)
                )
                if version is not None:
                    try:
                        wanted = int(version)
                    except (TypeError, ValueError):
                        return 0
                    stmt = stmt.where(ApplicationProfileORM.version == wanted)
                executed: Any = await session.execute(stmt)
                removed = int(executed.rowcount or 0)
        except Exception as exc:
            logger.exception(
                "profile delete failed",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "event": "profile.delete",
                        "outcome": "failed",
                        "application_id": application_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                ),
            )
            raise
        logger.info(
            "profile deleted",
            extra=sanitize_extra(
                {
                    "tenant_id": tenant_id,
                    "event": "profile.delete",
                    "outcome": "deleted",
                    "application_id": application_id,
                    "removed_count": removed,
                }
            ),
        )
        return removed
