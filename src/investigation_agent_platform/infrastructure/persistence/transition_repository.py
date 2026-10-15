# src/investigation_agent_platform/infrastructure/persistence/transition_repository.py
"""SQLAlchemy-backed durable investigation transition audit log (F-012)."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from investigation_agent_platform.api.errors import sanitize_extra
from investigation_agent_platform.infrastructure.persistence.models import (
    InvestigationTransitionORM,
)
from investigation_agent_platform.infrastructure.persistence.rls import rls_session

logger = logging.getLogger(__name__)


class SqlAlchemyTransitionRepository:
    """PostgreSQL-backed TransitionEventRepository over investigation_transitions."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    async def record_transition(
        self,
        tenant_id: str,
        investigation_id: UUID,
        from_state: str,
        to_state: str,
        reason: str,
        actor: str = "SYSTEM",
        _buffer_id: UUID | None = None,
    ) -> None:
        """Record a transition. ``actor`` carries the domain ``ActorType`` value
        (TO_DO-012: user cancellations must not be misattributed to the
        system); it defaults to ``"SYSTEM"`` so existing callers — including
        the port signature, which has no actor parameter — keep working.
        ``_buffer_id`` is set only by buffered-persistence replay so a
        retried replay re-inserts the same PK instead of duplicating the
        audit row; callers never pass it (the port signature is unchanged)."""
        if not actor or len(actor) > 32:
            raise ValueError("Transition actor must be a non-empty string of at most 32 chars")
        try:
            async with rls_session(self._session_factory, tenant_id) as session:
                row = InvestigationTransitionORM(
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    from_status=from_state,
                    to_status=to_state,
                    actor=actor,
                    reason=reason,
                )
                if _buffer_id is not None:
                    row.id = _buffer_id
                session.add(row)
        except Exception as exc:
            logger.exception(
                "transition record failed",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "investigation_id": str(investigation_id),
                        "event": "transition.record",
                        "outcome": "failed",
                        "from_state": from_state,
                        "to_state": to_state,
                        "actor": actor,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                ),
            )
            raise
        logger.info(
            "transition recorded",
            extra=sanitize_extra(
                {
                    "tenant_id": tenant_id,
                    "investigation_id": str(investigation_id),
                    "event": "transition.record",
                    "outcome": "recorded",
                    "from_state": from_state,
                    "to_state": to_state,
                    "actor": actor,
                }
            ),
        )
