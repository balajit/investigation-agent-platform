"""R10 (e) + (f): bank before you remember; delete the id-less branch.

(e) dedup marks roll back when the dispatch fails and truncated rows are
released. (f) id-less attempts are skipped (counter-only) and the producer
always stamps an id. Written first, red before the fix.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


def _item(fingerprint):  # type: ignore[no-untyped-def]
    return SimpleNamespace(fingerprint=fingerprint)


def test_r10_t13_retry_after_failed_save_rebanks() -> None:
    import asyncio

    from investigation_agent_platform.application.evidence.gateway import (
        AsyncEvidenceGateway,
        EvidenceDeduplicator,
    )

    assert hasattr(AsyncEvidenceGateway, "dedup_scope")
    assert hasattr(AsyncEvidenceGateway, "dedup_release")

    async def go():  # type: ignore[no-untyped-def]
        dedup = EvidenceDeduplicator(None)
        rows = [_item("fp-1"), _item("fp-2")]

        async def _attempt():  # type: ignore[no-untyped-def]
            seen = await dedup.deduplicate("t", list(rows), "inv")
            assert len(seen) == 2
            raise RuntimeError("save_batch failed")

        try:
            async with dedup.scope():
                await _attempt()
        except RuntimeError:
            pass
        # Negative control: without the rollback the retry returns zero rows.
        again = await dedup.deduplicate("t", list(rows), "inv")
        assert [i.fingerprint for i in again] == ["fp-1", "fp-2"]

    asyncio.run(go())


def test_r10_t14_truncated_rows_are_released() -> None:
    import asyncio

    from investigation_agent_platform.application.evidence.gateway import (
        EvidenceDeduplicator,
    )

    async def go():  # type: ignore[no-untyped-def]
        dedup = EvidenceDeduplicator(None)
        rows = [_item(f"fp-{i}") for i in range(5)]
        async with dedup.scope():
            seen = await dedup.deduplicate("t", list(rows), "inv")
            assert len(seen) == 5
            dedup.release("t", "inv", seen[2:])
        again = await dedup.deduplicate("t", list(rows), "inv")
        assert [i.fingerprint for i in again] == ["fp-2", "fp-3", "fp-4"]

    asyncio.run(go())


def test_r10_t15_successful_repeat_still_dedupes() -> None:
    import asyncio

    from investigation_agent_platform.application.evidence.gateway import (
        EvidenceDeduplicator,
    )

    async def go():  # type: ignore[no-untyped-def]
        dedup = EvidenceDeduplicator(None)
        rows = [_item("fp-1")]
        async with dedup.scope():
            assert len(await dedup.deduplicate("t", list(rows), "inv")) == 1
        async with dedup.scope():
            assert await dedup.deduplicate("t", list(rows), "inv") == []

    asyncio.run(go())


def test_r10_t16_idless_attempts_skipped_producer_stamps_id() -> None:
    from investigation_agent_platform.application.evidence import retrieval_ledger
    from investigation_agent_platform.application.evidence.retrieval_ledger import (
        count_persisted_attempts,
        merge_recorded_attempts,
    )

    before = int(getattr(retrieval_ledger, "_IDLESS_ATTEMPTS_SKIPPED", 0))
    idless = [([], {"search_query": "q", "scope": "s"})]
    assert count_persisted_attempts(idless, "q", "s") == 0
    merged = merge_recorded_attempts(
        [], [([], {"search_query": "q", "scope": "s"})]
    )
    assert merged == []
    after = int(getattr(retrieval_ledger, "_IDLESS_ATTEMPTS_SKIPPED", 0))
    assert after - before == 2
    # An id still counts and still joins.
    assert (
        count_persisted_attempts(
            [([], {"search_query": "q", "scope": "s", "attempt_id": "a1"})],
            "q", "s",
        )
        == 1
    )


@pytest.mark.asyncio
async def test_r10_t16_execute_without_iteration_stamps_deterministic_id() -> None:
    from investigation_agent_platform.api.dependencies import AppContext, set_app_context
    from investigation_agent_platform.application.worker.activities import (
        ExecuteActionInput,
        execute_action_activity,
    )

    markers: list = []
    saved: list = []

    async def _find(tenant_id, investigation_id):  # type: ignore[no-untyped-def]
        return []

    async def _save_batch(tenant_id, items, investigation_id):  # type: ignore[no-untyped-def]
        saved.extend(items)

    async def _search(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
        return SimpleNamespace(items=[SimpleNamespace(id=1, fingerprint="fp-1")])

    from contextlib import asynccontextmanager

    class _Gateway:
        async def search_runtime_evidence(self, tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            return await _search(tenant_id, investigation_id, application_id, req)

        def dedup_scope(self):  # type: ignore[no-untyped-def]
            @asynccontextmanager
            async def _scope():  # type: ignore[no-untyped-def]
                yield

            return _scope()

        def dedup_release(self, tenant_id, investigation_id, items):  # type: ignore[no-untyped-def]
            return None

    async def _append(tenant_id, event, investigation_id):  # type: ignore[no-untyped-def]
        markers.append(event)

    service = SimpleNamespace(
        authorize=AsyncMock(return_value=None),
        execute=AsyncMock(return_value=None),
    )
    ctx = AppContext()
    ctx.execute_action_service = lambda: service  # type: ignore[method-assign]
    ctx.action_repo = SimpleNamespace(count_by_investigation=AsyncMock(return_value=0))
    ctx.evidence_repo = SimpleNamespace(
        find_by_investigation_id=_find, save_batch=_save_batch
    )
    ctx.profile_repo = SimpleNamespace(get_by_application_id=AsyncMock(return_value=None))
    ctx.evidence_gateway = _Gateway()
    ctx.timeline_repo = SimpleNamespace(append=_append)
    ctx.investigation_repo = SimpleNamespace(
        get_by_id=AsyncMock(return_value=SimpleNamespace(application_id=None, metadata={}))
    )
    set_app_context(ctx)
    result = await execute_action_activity(
        ExecuteActionInput(
            tenant_id="t",
            investigation_id=str(uuid4()),
            action="SEARCH_LOGS",
            parameters={"keywords": ["kw"]},
        )
    )
    assert result.success is True
    assert markers, "expected a RETRIEVAL_ATTEMPT marker"
    action_id = result.data["action_outcome"]["action_id"]
    assert markers[0].attributes["attempt_id"] == f"act-{action_id}"
