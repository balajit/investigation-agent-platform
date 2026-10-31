"""window_facts: bank-time enumeration over a file-head window (synthetic only).

Proves the consolidation's core: a plain file-only range read with an
unclosed tail (the provider's byte cap) still yields RESOLVED records
for the cleanly-declared method and field inside it — no method_name
parameter, no forcing turn, no per-case trigger.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from investigation_agent_platform.application.analysis.java.window_facts import (
    enumerate_java_window_facts,
)
from investigation_agent_platform.domain.code.models import (
    CodeFactStatus,
    CodeValueSemantics,
    JavaFactCategory,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)

_SOURCE = """package com.sample.orders;
public class OrderLookupServiceImpl {
    private String defaultStore = "MAIN";
    public String findOrder(String store) {
        if (!allowedStores.contains(store)) {
            return "STORE_NOT_SUPPORTED";
        }
        return orderCache.fetch(store);
    }
    public String brokenTail(String x
"""


def _evidence(
    source,
    *,
    path="service/src/main/java/com/sample/orders/OrderLookupServiceImpl.java",
    revision="54d73240d6b5",
):
    iid, eid = uuid4(), uuid4()
    now = datetime.now(UTC)
    numbered = "\n".join(f"{i + 1}| {ln}" for i, ln in enumerate(source.splitlines()))
    end = max(1, len(source.splitlines()))
    return Evidence(
        evidence_id=eid,
        tenant_id="tenant-a",
        investigation_id=iid,
        evidence_type=EvidenceType.SOURCE_CODE,
        provider="pygit2-local",
        source=f"git://repo/{path}@{revision}",
        title="Java source range",
        summary="Pinned Java source",
        content_snippet=numbered,
        attributes={"read_mode": "range"},
        observed_at=now,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id="tenant-a",
            investigation_id=iid,
            provider_type="GIT",
            requested_provider_id="pygit2-local",
            actual_provider_id="pygit2-local",
            source_system="Git",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="GIT", operation="GET_SOURCE", normalized_query_hash="h"
            ),
            source_location=SourceLocation(
                system="Git",
                identifier=f"repo/{path}",
                file_path=path,
                line_start=1,
                line_end=end,
                revision=revision,
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=str(eid),
    )


def test_enumerates_method_return_and_field_from_file_head_window():
    records = enumerate_java_window_facts(_evidence(_SOURCE)).records
    by_subject = {r.subject_identity: r for r in records}
    method = by_subject.get("com.sample.orders.OrderLookupServiceImpl.findOrder")
    assert method is not None
    assert method.status == CodeFactStatus.RESOLVED
    assert method.fact_category == JavaFactCategory.SOURCE_VALUE_FACT
    assert method.value_semantics == CodeValueSemantics.GUARDED_RETURN_LITERAL
    assert method.resolved_value == "STORE_NOT_SUPPORTED"
    field = by_subject.get("OrderLookupServiceImpl.defaultStore")
    assert field is not None
    assert field.status == CodeFactStatus.RESOLVED
    assert field.resolved_value == "MAIN"
    assert field.value_semantics == CodeValueSemantics.EXPLICIT_INITIALIZER


def test_enumeration_fail_closed():
    from investigation_agent_platform.application.analysis.java.window_result import (
        WindowStatus,
    )

    refused = enumerate_java_window_facts(_evidence("not java at all {{{", revision="HEAD"))
    assert refused.status == WindowStatus.REFUSED
    assert refused.records == ()
    broken = SimpleNamespace(provenance=None, attributes={}, content_snippet="")
    failed = enumerate_java_window_facts(broken)
    assert failed.status == WindowStatus.REFUSED
    assert failed.records == ()


_NORMALIZE_MAPPER = """package com.sample.orders.legacy;
public class LegacyCodeMapper {
    public String normalize(String orderType) {
        if (orderType == null) {
            return "STANDARD";
        }
        return orderType.trim().toUpperCase();
    }
}
"""


def test_verify_backfills_shared_rows_without_bank_time_analysis():
    """End-to-end: a numbered pinned row with no v2 payload verifies via
    backfilled enumeration — the shared-row case (rows banked before
    enumeration existed, or reused across investigations)."""
    from types import SimpleNamespace

    from investigation_agent_platform.application.investigation.verification import (
        VerificationEngine,
        VerificationStatus,
    )
    from investigation_agent_platform.domain.code.models import JavaFactCategory

    ev = _evidence(
        _NORMALIZE_MAPPER,
        path="service/src/main/java/com/sample/orders/legacy/LegacyCodeMapper.java",
    )
    hyp = SimpleNamespace(
        id=uuid4(),
        statement='The normalize method returns "STANDARD" if orderType is null.',
        fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
        claimed_value="STANDARD",
        claim_target=SimpleNamespace(
            symbol="com.sample.orders.legacy.LegacyCodeMapper.normalize",
            file_path="service/src/main/java/com/sample/orders/legacy/LegacyCodeMapper.java",
        ),
        supporting_evidence_ids=[ev.evidence_id],
        assessments=[],
        required_verification=[],
    )
    result = VerificationEngine().validate_java_fact_claim(hyp, [ev], [])
    assert result is not None
    assert result.status == VerificationStatus.VERIFIED
    assert result.corroborating_evidence_ids == [ev.evidence_id]


def test_backfill_skips_rows_with_selector_analysis():
    """A row that already carries selector-driven v2 keeps it untouched."""
    from investigation_agent_platform.application.investigation.verification import (
        VerificationEngine,
    )

    ev = _evidence(
        _NORMALIZE_MAPPER,
        path="service/src/main/java/com/sample/orders/legacy/LegacyCodeMapper.java",
    )
    tagged = ev.model_copy(
        update={
            "attributes": {
                **ev.attributes,
                "code_fact_resolution_v2": {"status": "UNRESOLVED", "limitation_codes": ["x"]},
            }
        }
    )
    out = VerificationEngine._with_window_backfill([tagged])
    assert out[0].attributes["code_fact_resolution_v2"]["status"] == "UNRESOLVED"
    assert "code_fact_resolutions" not in out[0].attributes


def test_quoted_claimed_value_verifies_against_bare_resolution():
    """The model commits the literal with the quotes it sees; the
    analyzer resolves the bare value. Comparison canonicalizes once."""
    from types import SimpleNamespace
    from uuid import uuid4

    from investigation_agent_platform.application.investigation.verification import (
        VerificationEngine,
        VerificationStatus,
    )
    from investigation_agent_platform.domain.code.models import JavaFactCategory

    ev = _evidence(
        _NORMALIZE_MAPPER,
        path="service/src/main/java/com/sample/orders/legacy/LegacyCodeMapper.java",
    )
    hyp = SimpleNamespace(
        id=uuid4(),
        statement='The normalize method returns "STANDARD" if orderType is null.',
        fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
        claimed_value='"STANDARD"',
        claim_target=SimpleNamespace(
            symbol="com.sample.orders.legacy.LegacyCodeMapper.normalize",
            file_path="service/src/main/java/com/sample/orders/legacy/LegacyCodeMapper.java",
        ),
        supporting_evidence_ids=[ev.evidence_id],
        assessments=[],
        required_verification=[],
    )
    result = VerificationEngine().validate_java_fact_claim(hyp, [ev], [])
    assert result is not None
    assert result.status == VerificationStatus.VERIFIED


def test_bare_literal_strips_one_quote_layer():
    from investigation_agent_platform.application.investigation.verification import (
        VerificationEngine,
    )

    assert VerificationEngine._bare_literal('"X"') == "X"
    assert VerificationEngine._bare_literal("'X'") == "X"
    assert VerificationEngine._bare_literal("X") == "X"
    assert VerificationEngine._bare_literal("30") == "30"
    assert VerificationEngine._bare_literal(None) == ""


def test_backfill_treats_enumerated_row_as_proof_grade():
    """A discovery-labeled but numbered+pinned row verifies once its
    cleanly-declared values are enumerated — no selector read needed."""
    from types import SimpleNamespace
    from uuid import uuid4

    from investigation_agent_platform.application.investigation.verification import (
        VerificationEngine,
        VerificationStatus,
    )
    from investigation_agent_platform.domain.code.models import JavaFactCategory

    ev = _evidence(
        _NORMALIZE_MAPPER,
        path="service/src/main/java/com/sample/orders/legacy/LegacyCodeMapper.java",
    )
    discovery = ev.model_copy(update={"attributes": {**ev.attributes, "discovery_only": True}})
    hyp = SimpleNamespace(
        id=uuid4(),
        statement='The normalize method returns "STANDARD" if orderType is null.',
        fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
        claimed_value="STANDARD",
        claim_target=SimpleNamespace(
            symbol="com.sample.orders.legacy.LegacyCodeMapper.normalize",
            file_path="service/src/main/java/com/sample/orders/legacy/LegacyCodeMapper.java",
        ),
        supporting_evidence_ids=[ev.evidence_id],
        assessments=[],
        required_verification=[],
    )
    # Without backfill the discovery-labeled row carries no analysis.
    ok, reason = VerificationEngine().java_fact_proof(hyp, [discovery])
    assert ok is False
    assert reason == "no resolved CodeFactResolutionV2 is bound to the cited evidence"
    items = VerificationEngine._with_window_backfill([discovery])
    assert "discovery_only" not in (items[0].attributes or {})
    result = VerificationEngine().validate_java_fact_claim(hyp, items, [])
    assert result is not None
    assert result.status == VerificationStatus.VERIFIED
    # Banked row untouched.
    assert discovery.attributes.get("discovery_only") is True
    assert "code_fact_resolutions" not in discovery.attributes


def test_duplicate_coverage_of_same_fact_is_not_a_conflict():
    """Two overlapping reads resolving the same subject+value agree —
    the conflict check compares content, not row binding."""
    from types import SimpleNamespace
    from uuid import uuid4

    from investigation_agent_platform.application.investigation.verification import (
        VerificationEngine,
        VerificationStatus,
    )
    from investigation_agent_platform.domain.code.models import JavaFactCategory

    ev1 = _evidence(
        _NORMALIZE_MAPPER,
        path="service/src/main/java/com/sample/orders/legacy/LegacyCodeMapper.java",
    )
    ev2 = _evidence(
        _NORMALIZE_MAPPER,
        path="service/src/main/java/com/sample/orders/legacy/LegacyCodeMapper.java",
    )
    assert ev1.evidence_id != ev2.evidence_id
    hyp = SimpleNamespace(
        id=uuid4(),
        statement='The normalize method returns "STANDARD" if orderType is null.',
        fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
        claimed_value="STANDARD",
        claim_target=SimpleNamespace(
            symbol="com.sample.orders.legacy.LegacyCodeMapper.normalize",
            file_path="service/src/main/java/com/sample/orders/legacy/LegacyCodeMapper.java",
        ),
        supporting_evidence_ids=[ev1.evidence_id, ev2.evidence_id],
        assessments=[],
        required_verification=[],
    )
    engine = VerificationEngine()
    items = engine._with_window_backfill([ev1, ev2])
    ok, reason = engine.java_fact_proof(hyp, items)
    assert ok, reason
    result = engine.validate_java_fact_claim(hyp, items, [])
    assert result is not None
    assert result.status == VerificationStatus.VERIFIED
