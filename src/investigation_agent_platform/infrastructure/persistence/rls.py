# src/investigation_agent_platform/infrastructure/persistence/rls.py
"""RLS helper — SET LOCAL app.tenant_id per transaction."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@asynccontextmanager
async def rls_session(factory: Any, tenant_id: str) -> AsyncGenerator[AsyncSession]:
    """Yield session with RLS tenant set. Hard requirement for production."""
    async with factory() as session:
        await session.execute(text("SET LOCAL app.tenant_id = :tid"), {"tid": tenant_id})
        yield session
        await session.commit()
