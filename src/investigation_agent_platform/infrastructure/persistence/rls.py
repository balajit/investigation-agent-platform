# src/investigation_agent_platform/infrastructure/persistence/rls.py
"""RLS helper — SET LOCAL app.tenant_id per transaction."""

import logging
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from investigation_agent_platform.domain.common.exceptions import SecurityPolicyViolationException

logger = logging.getLogger(__name__)

_TENANT_RE = re.compile(r"^[A-Za-z0-9_.\-:]{1,128}$")


@asynccontextmanager
async def rls_session(factory: Any, tenant_id: str) -> AsyncGenerator[AsyncSession]:
    """Yield session with RLS tenant set. Hard requirement for production.

    On any exception raised by the caller, the transaction is explicitly
    rolled back before the session is closed (F-031) — a pooled connection
    must never be returned in an indeterminate transaction state, and a
    failed operation must never be silently committed.
    """
    # SET LOCAL takes no bind parameters in Postgres — inline a validated
    # literal instead. The charset whitelist makes quoting airtight.
    if not _TENANT_RE.fullmatch(tenant_id):
        raise SecurityPolicyViolationException(f"Unsafe tenant_id for RLS session: {tenant_id!r}")
    async with factory() as session:
        await session.execute(text(f"SET LOCAL app.tenant_id = '{tenant_id}'"))
        try:
            yield session
            await session.commit()
        except Exception:
            try:
                await session.rollback()
            except Exception:
                logger.exception("rls_session: rollback itself failed")
            raise
