# src/investigation_agent_platform/infrastructure/persistence/chat_repository.py
"""SQLAlchemy chat session/message repository (Part 11.10)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from investigation_agent_platform.domain.common.exceptions import ConcurrencyError
from investigation_agent_platform.domain.investigation.chat import (
    ChatMessage,
    ChatRole,
    ChatSessionStatus,
    InvestigationChatSession,
)
from investigation_agent_platform.infrastructure.persistence.models import (
    ChatMessageORM,
    ChatSessionORM,
)
from investigation_agent_platform.infrastructure.persistence.rls import rls_session


def _session_to_orm(session: InvestigationChatSession) -> ChatSessionORM:
    return ChatSessionORM(
        id=session.id,
        tenant_id=session.tenant_id,
        investigation_id=session.investigation_id,
        application_id=session.application_id,
        status=session.status.value,
        classification=session.classification,
        allowed_provider=session.allowed_provider,
        authorization_reference=session.authorization_reference,
        legal_hold=session.legal_hold,
        contract_version=session.contract_version,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def _session_from_orm(row: ChatSessionORM) -> InvestigationChatSession:
    return InvestigationChatSession(
        id=row.id,
        tenant_id=row.tenant_id,
        investigation_id=row.investigation_id,
        application_id=row.application_id,
        status=ChatSessionStatus(row.status),
        classification=row.classification,
        allowed_provider=row.allowed_provider,
        authorization_reference=row.authorization_reference,
        legal_hold=row.legal_hold,
        contract_version=row.contract_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _message_to_orm(message: ChatMessage) -> ChatMessageORM:
    return ChatMessageORM(
        id=message.id,
        tenant_id=message.tenant_id,
        session_id=message.session_id,
        role=message.role.value,
        content=message.content,
        prompt_tokens=message.prompt_tokens,
        completion_tokens=message.completion_tokens,
        estimated_cost_usd=message.estimated_cost_usd,
        provenance_json=(
            message.provenance.model_dump(mode="json") if message.provenance is not None else None
        ),
        created_at=message.created_at,
    )


def _message_from_orm(row: ChatMessageORM) -> ChatMessage:
    from investigation_agent_platform.domain.common.provenance import ProvenanceRecord

    return ChatMessage(
        id=row.id,
        tenant_id=row.tenant_id,
        session_id=row.session_id,
        role=ChatRole(row.role),
        content=row.content,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        estimated_cost_usd=row.estimated_cost_usd,
        provenance=(
            ProvenanceRecord.model_validate(row.provenance_json) if row.provenance_json else None
        ),
        created_at=row.created_at,
    )


class SqlAlchemyChatSessionRepository:
    """PostgreSQL-backed chat repository with strict tenant scoping."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    async def save_session(self, tenant_id: str, session: InvestigationChatSession) -> None:
        if session.tenant_id != tenant_id:
            raise ConcurrencyError("Chat session tenant mismatch")
        async with rls_session(self._session_factory, tenant_id) as db:
            existing = await db.get(ChatSessionORM, session.id)
            if existing is None:
                db.add(_session_to_orm(session))
            else:
                if existing.tenant_id != tenant_id:
                    raise ConcurrencyError("Chat session tenant mismatch")
                for attr, value in (
                    ("status", session.status.value),
                    ("legal_hold", session.legal_hold),
                    ("updated_at", session.updated_at),
                ):
                    setattr(existing, attr, value)

    async def get_session(
        self, tenant_id: str, session_id: UUID
    ) -> InvestigationChatSession | None:
        async with rls_session(self._session_factory, tenant_id) as db:
            row = await db.get(ChatSessionORM, session_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return _session_from_orm(row)

    async def save_message(self, tenant_id: str, message: ChatMessage) -> None:
        if message.tenant_id != tenant_id:
            raise ConcurrencyError("Chat message tenant mismatch")
        async with rls_session(self._session_factory, tenant_id) as db:
            db.add(_message_to_orm(message))

    async def list_messages(
        self, tenant_id: str, session_id: UUID, limit: int = 100, offset: int = 0
    ) -> tuple[list[ChatMessage], int]:
        async with rls_session(self._session_factory, tenant_id) as db:
            total = (
                await db.scalar(
                    select(func.count())
                    .select_from(ChatMessageORM)
                    .where(
                        ChatMessageORM.tenant_id == tenant_id,
                        ChatMessageORM.session_id == session_id,
                    )
                )
            ) or 0
            rows = (
                await db.scalars(
                    select(ChatMessageORM)
                    .where(
                        ChatMessageORM.tenant_id == tenant_id,
                        ChatMessageORM.session_id == session_id,
                    )
                    .order_by(ChatMessageORM.created_at.asc())
                    .limit(max(1, min(limit, 500)))
                    .offset(max(0, offset))
                )
            ).all()
            return [_message_from_orm(row) for row in rows], int(total)

    async def count_active_sessions(self, tenant_id: str) -> int:
        async with rls_session(self._session_factory, tenant_id) as db:
            return (
                await db.scalar(
                    select(func.count())
                    .select_from(ChatSessionORM)
                    .where(
                        ChatSessionORM.tenant_id == tenant_id,
                        ChatSessionORM.status == ChatSessionStatus.ACTIVE.value,
                    )
                )
            ) or 0

    async def delete_session(self, tenant_id: str, session_id: UUID) -> None:
        async with rls_session(self._session_factory, tenant_id) as db:
            row = await db.get(ChatSessionORM, session_id)
            if row is None or row.tenant_id != tenant_id:
                return
            messages = (
                await db.scalars(
                    select(ChatMessageORM).where(
                        ChatMessageORM.tenant_id == tenant_id,
                        ChatMessageORM.session_id == session_id,
                    )
                )
            ).all()
            for message in messages:
                await db.delete(message)
            await db.delete(row)

    async def sessions_older_than(
        self, tenant_id: str, cutoff: datetime, limit: int = 500
    ) -> list[InvestigationChatSession]:
        async with rls_session(self._session_factory, tenant_id) as db:
            rows = (
                await db.scalars(
                    select(ChatSessionORM)
                    .where(
                        ChatSessionORM.tenant_id == tenant_id,
                        ChatSessionORM.created_at <= cutoff,
                    )
                    .order_by(ChatSessionORM.created_at.asc())
                    .limit(max(1, min(limit, 500)))
                )
            ).all()
            return [_session_from_orm(row) for row in rows]
