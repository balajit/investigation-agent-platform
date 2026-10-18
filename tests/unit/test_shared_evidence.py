# tests/unit/test_shared_evidence.py
"""G6 shareable code evidence: outcomes, hydration union, tenant isolation (no DB)."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from investigation_agent_platform.api.dependencies import InMemoryEvidenceRepository
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)


def run(coro):
    return asyncio.run(coro)


def _evidence(fp, etype=EvidenceType.SOURCE_CODE, tenant="n-a", summary="code"):
    iid = uuid4()
    now = datetime.now(UTC)
    return Evidence(
        tenant_id=tenant,
        investigation_id=iid,
        evidence_type=etype,
        provider="test",
        source="test://src",
        title="t",
        summary=summary,
        content_snippet="snippet",
        observed_at=now,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id=tenant,
            investigation_id=iid,
            provider_type="test",
            requested_provider_id="test",
            actual_provider_id="test",
            source_system="test",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="test", operation="GET_SOURCE", normalized_query_hash="abc"
            ),
            source_location=SourceLocation(system="test", identifier="x"),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=fp,
    )


def test_reference_conflict_records_interest_and_hydrates():
    repo = InMemoryEvidenceRepository()
    inv_a, inv_b = uuid4(), uuid4()
    row = _evidence("fp-shared-1")
    assert run(repo.save("n-a", row, inv_a)) == "inserted"
    twin = _evidence("fp-shared-1")
    assert run(repo.save("n-a", twin, inv_b)) == "shared"
    hydrated = run(repo.find_by_investigation_id("n-a", inv_b))
    assert [e.evidence_id for e in hydrated] == [row.evidence_id]
    info = run(repo.sharing_info("n-a", [str(row.evidence_id)]))
    assert info[str(row.evidence_id)]["shared"] is True
    assert str(inv_b) in info[str(row.evidence_id)]["investigations"]


def test_same_investigation_conflict_stays_deduplicated():
    repo = InMemoryEvidenceRepository()
    inv = uuid4()
    assert run(repo.save("n-a", _evidence("fp-x"), inv)) == "inserted"
    assert run(repo.save("n-a", _evidence("fp-x"), inv)) == "deduplicated"
    assert run(repo.find_by_investigation_id("n-a", inv)).__len__() == 1


def test_observational_conflict_never_shared():
    repo = InMemoryEvidenceRepository()
    inv_a, inv_b = uuid4(), uuid4()
    assert run(repo.save("n-a", _evidence("fp-log", EvidenceType.LOG), inv_a)) == "inserted"
    assert run(repo.save("n-a", _evidence("fp-log", EvidenceType.LOG), inv_b)) == "deduplicated"
    assert run(repo.find_by_investigation_id("n-a", inv_b)) == []
    assert run(repo.sharing_info("n-a", ["anything"])) == {}


def test_tenant_isolation():
    repo = InMemoryEvidenceRepository()
    inv_a, inv_b = uuid4(), uuid4()
    row = _evidence("fp-t", tenant="t1")
    assert run(repo.save("t1", row, inv_a)) == "inserted"
    # Same fingerprint, other tenant: independent row, never shared.
    other = _evidence("fp-t", tenant="t2")
    assert run(repo.save("t2", other, inv_b)) == "inserted"
    assert run(repo.find_by_investigation_id("t2", inv_b)) == [other]
    assert run(repo.sharing_info("t1", [str(row.evidence_id)])) == {}


def test_save_batch_counts():
    repo = InMemoryEvidenceRepository()
    inv_a, inv_b = uuid4(), uuid4()
    run(repo.save_batch("n-a", [_evidence("fp-1"), _evidence("fp-2")], inv_a))
    counts = run(
        repo.save_batch(
            "n-a",
            [_evidence("fp-1"), _evidence("fp-3", EvidenceType.LOG), _evidence("fp-1")],
            inv_b,
        )
    )
    assert counts["shared"] == 1
    assert counts["inserted"] == 1  # fp-3 log row persisted own
    assert counts["deduplicated"] == 0
    hydrated = run(repo.find_by_investigation_id("n-a", inv_b))
    assert len(hydrated) == 2


def test_sql_shared_records_interest():
    from unittest.mock import AsyncMock, MagicMock, patch

    from investigation_agent_platform.infrastructure.persistence.evidence_repository import (
        SqlAlchemyEvidenceRepository,
    )

    other_inv = uuid4()
    insert_result = MagicMock()
    insert_result.rowcount = 0
    holder_result = MagicMock()
    holder_result.all.return_value = [("fp-s", other_inv, "key-s")]
    interest_result = MagicMock()
    interest_result.rowcount = 1
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[insert_result, holder_result, interest_result])
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)
    with patch(
        "investigation_agent_platform.infrastructure.persistence.evidence_repository.rls_session",
        return_value=cm,
    ):
        repo = SqlAlchemyEvidenceRepository(db_session_factory=MagicMock())
        counts = run(repo.save_batch("n-a", [_evidence("fp-s")], uuid4()))
    assert counts == {"inserted": 0, "shared": 1, "deduplicated": 0}
    assert session.execute.await_count == 3


def test_sql_save_conflict_records_interest():
    from unittest.mock import AsyncMock, MagicMock, patch

    from investigation_agent_platform.infrastructure.persistence.evidence_repository import (
        SqlAlchemyEvidenceRepository,
    )

    other_inv = uuid4()
    writer_inv = uuid4()
    insert_result = MagicMock()
    insert_result.rowcount = 0
    holder_result = MagicMock()
    holder_result.first.return_value = (other_inv, "key-canon")
    interest_result = MagicMock()
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[insert_result, holder_result, interest_result])
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)
    with patch(
        "investigation_agent_platform.infrastructure.persistence.evidence_repository.rls_session",
        return_value=cm,
    ):
        repo = SqlAlchemyEvidenceRepository(db_session_factory=MagicMock())
        outcome = run(repo.save("n-a", _evidence("fp-s"), writer_inv))
    assert outcome == "shared"
    assert session.execute.await_count == 3
