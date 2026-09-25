# src/investigation_agent_platform/infrastructure/persistence/profile_repository.py
"""SQLAlchemy application profile repository."""

from typing import Any

from sqlalchemy import select

from investigation_agent_platform.domain.profile.models import ApplicationProfile
from investigation_agent_platform.infrastructure.persistence.models import ApplicationProfileORM


class SqlAlchemyApplicationProfileRepository:
    """PostgreSQL-backed ApplicationProfileRepository over the application_profiles table."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    @staticmethod
    def _to_orm(profile: ApplicationProfile) -> ApplicationProfileORM:
        return ApplicationProfileORM(
            id=profile.id,
            name=profile.name,
            description=profile.description,
            environment=profile.environment,
            profile_json=profile.model_dump(mode="json", by_alias=True),
        )

    @staticmethod
    def _from_orm(row: ApplicationProfileORM) -> ApplicationProfile:
        return ApplicationProfile.model_validate(row.profile_json)

    async def get_by_application_id(self, application_id: str) -> ApplicationProfile | None:
        async with self._session_factory() as session:
            result = await session.scalars(
                select(ApplicationProfileORM).where(ApplicationProfileORM.id == application_id)
            )
            row = result.first()
            if row is None:
                return None
            return self._from_orm(row)

    async def list(self) -> list[ApplicationProfile]:
        async with self._session_factory() as session:
            result = await session.scalars(select(ApplicationProfileORM))
            return [self._from_orm(row) for row in result.all()]

    async def save(self, profile: ApplicationProfile) -> None:
        orm = self._to_orm(profile)
        async with self._session_factory() as session:
            existing = await session.scalars(
                select(ApplicationProfileORM).where(ApplicationProfileORM.id == profile.id)
            )
            row = existing.first()
            if row is not None:
                row.name = orm.name
                row.description = orm.description
                row.environment = orm.environment
                row.profile_json = orm.profile_json
                await session.commit()
                return
            session.add(orm)
            await session.commit()