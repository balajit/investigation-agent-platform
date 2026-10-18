# tests/unit/test_part10_phase10.py
"""Part 10 Phase 10: get-path mapping scoping (no hardcoded index prefix)."""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from investigation_agent_platform.domain.common.exceptions import EvidenceNotFoundException
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)
from investigation_agent_platform.infrastructure.configuration.mapping_registry import (
    load_mapping_profiles,
)
from investigation_agent_platform.infrastructure.evidence.runtime.elastic import (
    AsyncElasticAdapter,
    ElasticAdapterSettings,
)

SHIPPED_DIR = Path(__file__).resolve().parents[2] / "config" / "observability-mappings"
TENANT = "tenant-a"


def _mapping_row(tenant_id=TENANT, investigation_id=None, evidence_id=None, source=None):  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    iid = investigation_id or uuid.uuid4()
    return Evidence(
        tenant_id=tenant_id,
        investigation_id=iid,
        evidence_id=evidence_id or uuid.uuid4(),
        evidence_type=EvidenceType.RUNTIME_LOG,
        provider="ELASTIC",
        source=source or "elasticsearch://logs-tenant-a-1-production-1/doc1",
        title="t",
        summary="s",
        fingerprint="doc1",
        observed_at=now,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id=tenant_id,
            investigation_id=iid,
            provider_type="ELASTIC",
            requested_provider_id="elastic-primary",
            actual_provider_id="elastic-primary",
            source_system="Elasticsearch",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="ELASTIC", operation="SEARCH", normalized_query_hash="f" * 64
            ),
            source_location=SourceLocation(system="Elasticsearch", identifier="doc1"),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
    )


class _Lookup:
    def __init__(self, evidence=None):  # type: ignore[no-untyped-def]
        self._evidence = evidence

    async def get_evidence(self, tenant_id, evidence_id):  # type: ignore[no-untyped-def]
        return self._evidence


def _adapter(client=None, registry=True):  # type: ignore[no-untyped-def]
    return AsyncElasticAdapter(
        client=client or MagicMock(),
        settings=ElasticAdapterSettings(cursor_signing_key=b"test-signing-key"),
        mapping_registry=load_mapping_profiles(SHIPPED_DIR) if registry else None,
    )


def _hit(doc_id="doc1", index="logz-gen-verbose-prod-es-003400"):  # type: ignore[no-untyped-def]
    return {
        "_id": doc_id,
        "_index": index,
        "_source": {"@timestamp": "2026-01-01T00:00:00Z", "message": "m"},
        "sort": [1767225600000, doc_id],
    }


@pytest.mark.asyncio
async def test_get_resolves_gen_index_under_mapping():
    iid = uuid.uuid4()
    eid = uuid.uuid4()
    row = _mapping_row(
        investigation_id=iid,
        evidence_id=eid,
        source="elasticsearch://logz-gen-verbose-prod-es-003400/doc1",
    )
    client = MagicMock()
    client.search = AsyncMock(return_value={"hits": {"hits": [_hit()], "total": {"value": 1}}})
    adapter = _adapter(client=client)
    ev = await adapter.get_runtime_evidence(
        TENANT, iid, eid, "production", _Lookup(row), mapping_source_id="gen-verbose"
    )
    assert ev.investigation_id == iid
    (_, kwargs) = client.search.call_args
    assert kwargs["index"] == "logz-gen-verbose-prod-es-003400"
    assert ev.attributes.get("message") == "m"


@pytest.mark.asyncio
async def test_get_cross_pattern_index_rejected_without_io():
    iid = uuid.uuid4()
    eid = uuid.uuid4()
    row = _mapping_row(
        investigation_id=iid,
        evidence_id=eid,
        source="elasticsearch://logz-gen-verbose-prod-es-003400/doc1",
    )
    client = MagicMock()
    client.search = AsyncMock()
    adapter = _adapter(client=client)
    with pytest.raises(EvidenceNotFoundException):
        await adapter.get_runtime_evidence(
            TENANT, iid, eid, "production", _Lookup(row), mapping_source_id="gen-root-cause"
        )
    client.search.assert_not_called()


@pytest.mark.asyncio
async def test_get_legacy_prefix_path_intact_when_omitted():
    iid = uuid.uuid4()
    eid = uuid.uuid4()
    row = _mapping_row(investigation_id=iid, evidence_id=eid)
    client = MagicMock()
    client.search = AsyncMock(
        return_value={
            "hits": {
                "hits": [_hit("doc1", "logs-tenant-a-1-production-1")],
                "total": {"value": 1},
            }
        }
    )
    adapter = _adapter(client=client)
    ev = await adapter.get_runtime_evidence(TENANT, iid, eid, "production", _Lookup(row))
    assert ev.investigation_id == iid


@pytest.mark.asyncio
async def test_fetch_fingerprint_binds_mapping():
    iid = uuid.uuid4()
    eid = uuid.uuid4()
    row = _mapping_row(
        investigation_id=iid,
        evidence_id=eid,
        source="elasticsearch://logz-gen-verbose-prod-es-003400/doc1",
    )
    client = MagicMock()
    client.search = AsyncMock(return_value={"hits": {"hits": [_hit()], "total": {"value": 1}}})
    adapter = _adapter(client=client)
    ev = await adapter.get_runtime_evidence(
        TENANT, iid, eid, "production", _Lookup(row), mapping_source_id="gen-verbose"
    )
    expected = hashlib.sha256(
        json.dumps(
            {
                "op": "GET",
                "tenant_id": TENANT,
                "investigation_id": str(iid),
                "index": "logz-gen-verbose-prod-es-003400",
                "document_id": "doc1",
                "mapping_source_id": "gen-verbose",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert ev.provenance.query_fingerprint.normalized_query_hash == expected
    assert ev.provenance.query_fingerprint.operation == "GET"
