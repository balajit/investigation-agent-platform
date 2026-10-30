# tests/unit/test_dev_seed.py
"""Dev-seed conflict tolerance: existence checks race across restarts, so a
duplicate-key conflict means the row is already there — count as resumed,
never crash the seed (and never journal doomed WAL entries that end HELD).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from investigation_agent_platform.api.dev_seed import seed_dev_tenant
from investigation_agent_platform.domain.common.exceptions import ConcurrencyError


def _ctx_with(write_error=None):  # type: ignore[no-untyped-def]
    async def _fail(*args, **kwargs):  # type: ignore[no-untyped-def]
        if write_error is not None:
            raise write_error

    ctx = SimpleNamespace(
        profile_repo=SimpleNamespace(
            get_by_application_id=AsyncMock(return_value=None), save=_fail
        ),
        investigation_repo=SimpleNamespace(get_by_id=AsyncMock(return_value=None), create=_fail),
        evidence_repo=SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[]), save=_fail
        ),
        timeline_repo=SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[]), append=_fail
        ),
        hypothesis_repo=SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[]), save=_fail
        ),
        finding_repo=SimpleNamespace(
            list_findings=AsyncMock(return_value=([], 0)),
            save_finding_record=_fail,
            save_conclusion=_fail,
        ),
        input_repo=SimpleNamespace(
            get_pending=AsyncMock(return_value=[]), create_requirement=_fail
        ),
        artifact_repo=SimpleNamespace(
            list_for_investigation=AsyncMock(return_value=[]), save=_fail
        ),
        session_repo=SimpleNamespace(count_own_sessions=AsyncMock(return_value=0), append=_fail),
        background_job_repo=SimpleNamespace(
            list_jobs=AsyncMock(return_value=([], 0)), create=_fail
        ),
        finding_cluster_repo=SimpleNamespace(
            load_taxonomy=AsyncMock(return_value=None),
            create_cluster=_fail,
            assign_finding=_fail,
        ),
    )
    return ctx


class TestSeedConflictTolerance:
    @pytest.mark.asyncio
    async def test_conflicts_count_as_resumed(self) -> None:
        ctx = _ctx_with(write_error=ConcurrencyError("duplicate"))
        counts = await seed_dev_tenant(ctx, "tenant-a")
        assert counts["resumed"] > 0

    @pytest.mark.asyncio
    async def test_genuine_errors_still_raise(self) -> None:
        ctx = _ctx_with(write_error=ValueError("boom"))
        with pytest.raises(ValueError, match="boom"):
            await seed_dev_tenant(ctx, "tenant-a")
