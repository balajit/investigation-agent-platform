# src/investigation_agent_platform/infrastructure/persistence/profile_repository.py
"""SQLAlchemy application profile repository (F-033/F-034: tenant-aware contract, versioned)."""

from typing import Any

from sqlalchemy import select

from investigation_agent_platform.domain.profile.models import ApplicationProfile
from investigation_agent_platform.infrastructure.persistence.models import ApplicationProfileORM
from investigation_agent_platform.infrastructure.persistence.rls import rls_session


class SqlAlchemyApplicationProfileRepository:
    """PostgreSQL-backed ApplicationProfileRepository over the application_profiles table."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    @staticmethod
    def _to_orm(profile: ApplicationProfile, tenant_id: str) -> ApplicationProfileORM:
        return ApplicationProfileORM(
            id=profile.id,
            tenant_id=tenant_id,
            name=profile.name,
            description=profile.description,
            environment=profile.environment,
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
            result = await session.scalars(
                select(ApplicationProfileORM).where(
                    ApplicationProfileORM.id == application_id,
                    ApplicationProfileORM.tenant_id == tenant_id,
                )
            )
            row = result.first()
            if row is None:
                return None
            profile = self._from_orm(row)
            # F-034: exact version resolution — return None unless the stored
            # revision matches the requested version, so callers can distinguish
            # "wrong version" from "profile missing".
            if version is not None and str(profile.version) != str(version):
                return None
            return profile

    async def list(self, tenant_id: str) -> list[ApplicationProfile]:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(ApplicationProfileORM).where(ApplicationProfileORM.tenant_id == tenant_id)
            )
            return [self._from_orm(row) for row in result.all()]

    async def save(self, tenant_id: str, profile: ApplicationProfile) -> None:
        orm = self._to_orm(profile, tenant_id)
        async with rls_session(self._session_factory, tenant_id) as session:
            existing = await session.scalars(
                select(ApplicationProfileORM).where(
                    ApplicationProfileORM.id == profile.id,
                    ApplicationProfileORM.tenant_id == tenant_id,
                )
            )
            row = existing.first()
            if row is not None:
                row.name = orm.name
                row.description = orm.description
                row.environment = orm.environment
                row.profile_json = orm.profile_json
                return
            session.add(orm)
