# src/investigation_agent_platform/infrastructure/persistence/action_execution_repository.py
"""SQLAlchemy-backed durable action execution audit repository (F-004/F-068)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from investigation_agent_platform.domain.investigation.models import InvestigationAction
from investigation_agent_platform.infrastructure.persistence.models import ActionExecutionORM
from investigation_agent_platform.infrastructure.persistence.rls import rls_session


class SqlAlchemyActionExecutionRepository:
    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    async def record_action(
        self,
        tenant_id: str,
        investigation_id: UUID,
        action: InvestigationAction,
        result_status: str,
        principal_id: str = "worker",
        policy_version: str = "v1",
    ) -> None:
        # F-057: idempotent under Temporal retry — same action_id records once.
        async with rls_session(self._session_factory, tenant_id) as session:
            stmt = (
                pg_insert(ActionExecutionORM)
                .values(
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    action_id=action.action_id,
                    action_type=action.action_type.value,
                    principal_id=principal_id,
                    policy_version=policy_version,
                    result_status=result_status,
                )
                .on_conflict_do_nothing(index_elements=["action_id"])
            )
            await session.execute(stmt)

    async def count_by_investigation(self, tenant_id: str, investigation_id: UUID) -> int:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(func.count(ActionExecutionORM.id)).where(
                    ActionExecutionORM.tenant_id == tenant_id,
                    ActionExecutionORM.investigation_id == investigation_id,
                )
            )
            return int(result.first() or 0)
