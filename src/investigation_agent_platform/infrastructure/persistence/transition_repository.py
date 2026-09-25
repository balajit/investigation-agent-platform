# src/investigation_agent_platform/infrastructure/persistence/transition_repository.py
"""SQLAlchemy-backed durable investigation transition audit log (F-012)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from investigation_agent_platform.infrastructure.persistence.models import (
    InvestigationTransitionORM,
)
from investigation_agent_platform.infrastructure.persistence.rls import rls_session


class SqlAlchemyTransitionRepository:
    """PostgreSQL-backed TransitionEventRepository over investigation_transitions."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    async def record_transition(
        self, tenant_id: str, investigation_id: UUID, from_state: str, to_state: str, reason: str
    ) -> None:
        async with rls_session(self._session_factory, tenant_id) as session:
            session.add(
                InvestigationTransitionORM(
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    from_status=from_state,
                    to_status=to_state,
                    actor="SYSTEM",
                    reason=reason,
                )
            )
