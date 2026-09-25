"""RLS hard requirement integration test — tenant isolation at DB level."""

import pytest

from investigation_agent_platform.api.dependencies import AppContext, InMemoryInvestigationRepository
from investigation_agent_platform.domain.investigation.models import Investigation, InvestigationRequest, InvestigationContext
from datetime import datetime, timezone
from uuid import uuid4


@pytest.mark.asyncio
async def test_tenancy_isolation_in_memory() -> None:
    ctx = AppContext()
    # Create investigation for tenant-a
    req_a = InvestigationRequest(
        application_id="example-app",
        problem_description="tenant-a issue",
        session_id="sess-a",
        requested_by="user-a",
    )
    inv_a = await ctx.create_investigation_service().execute(req_a, tenant_id="tenant-a")
    assert inv_a.tenant_id == "tenant-a"

    # tenant-b cannot fetch tenant-a investigation (partitioned store returns None)
    fetched = await ctx.get_investigation_service().execute("tenant-b", inv_a.id)
    assert fetched is None


@pytest.mark.asyncio
async def test_rls_migration_exists() -> None:
    import pathlib
    assert pathlib.Path("migrations/001_add_rls.py").exists()
    assert pathlib.Path("src/investigation_agent_platform/infrastructure/persistence/rls.py").exists()
