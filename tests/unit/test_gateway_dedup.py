# tests/unit/test_gateway_dedup.py
"""Gateway deduplicator is scoped per investigation (Phase 4 fix).

A repeat query in a LATER investigation must return its rows: evidence
rows are investigation-scoped and citations resolve per investigation,
so cross-investigation suppression corrupts runs (it presented as
absence — every repeat search banked zero rows). Within one
investigation, repeats still dedupe.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from investigation_agent_platform.application.evidence.gateway import (
    EvidenceDeduplicator,
)


def _row(fingerprint):
    return SimpleNamespace(fingerprint=fingerprint, evidence_id=uuid4())


@pytest.mark.asyncio
async def test_repeat_within_investigation_dedupes():
    dedup = EvidenceDeduplicator()
    iid = uuid4()
    first = await dedup.deduplicate("t", [_row("fp-1")], iid)
    second = await dedup.deduplicate("t", [_row("fp-1")], iid)
    assert len(first) == 1
    assert second == []


@pytest.mark.asyncio
async def test_same_query_later_investigation_kept():
    """The live total-failure root cause: probe banks hits, next
    investigation's identical search must NOT come back empty."""
    dedup = EvidenceDeduplicator()
    first = await dedup.deduplicate("n-batt", [_row("fp-1")], uuid4())
    second = await dedup.deduplicate("n-batt", [_row("fp-1")], uuid4())
    assert len(first) == 1
    assert len(second) == 1


@pytest.mark.asyncio
async def test_tenants_isolated():
    dedup = EvidenceDeduplicator()
    iid = uuid4()
    assert len(await dedup.deduplicate("t1", [_row("fp-1")], iid)) == 1
    assert len(await dedup.deduplicate("t2", [_row("fp-1")], iid)) == 1


@pytest.mark.asyncio
async def test_batch_duplicates_dropped():
    dedup = EvidenceDeduplicator()
    out = await dedup.deduplicate("t", [_row("fp-1"), _row("fp-1")], uuid4())
    assert len(out) == 1


def test_legacy_call_without_investigation_uses_tenant_scope():
    import asyncio

    dedup = EvidenceDeduplicator()
    first = asyncio.run(dedup.deduplicate("t", [_row("fp-1")]))
    second = asyncio.run(dedup.deduplicate("t", [_row("fp-1")]))
    assert len(first) == 1
    assert second == []


def test_history_capped():
    dedup = EvidenceDeduplicator()
    dedup.MAX_REMEMBERED = 3
    import asyncio

    async def _fill():
        for i in range(5):
            await dedup.deduplicate("t", [_row(f"fp-{i}")], uuid4())

    asyncio.run(_fill())
    assert len(dedup._seen) == 3


def test_sync_variant_scoped():
    dedup = EvidenceDeduplicator()
    iid_a, iid_b = uuid4(), uuid4()
    assert len(dedup.deduplicate_sync("t", [_row("fp-1")], iid_a)) == 1
    assert len(dedup.deduplicate_sync("t", [_row("fp-1")], iid_a)) == 0
    assert len(dedup.deduplicate_sync("t", [_row("fp-1")], iid_b)) == 1
