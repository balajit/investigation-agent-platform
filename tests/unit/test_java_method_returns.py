"""Java method guarded-return resolution (live L5 class).

Covers resolve_java_method_return_fact: method-return-on-guard claims
(e.g. "if orderType is null, LegacyCodeMapper.normalize returns
'STANDARD'") that the field-only pipeline can never resolve, plus the
end-to-end v2 SOURCE_VALUE_FACT verification of such a claim.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from investigation_agent_platform.application.analysis.java.method_return_facts import (
    resolve_java_method_return_fact,
)
from investigation_agent_platform.application.investigation.verification import (
    VerificationEngine,
)
from investigation_agent_platform.domain.code.models import (
    CODE_FACT_RESOLUTION_V2_KEY,
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

_MAPPER = """package com.sample.orders.legacy;
public class LegacyCodeMapper {
    public String normalize(String orderType) {
        if (orderType == null) {
            return "STANDARD";
        }
        return orderType.trim().toUpperCase();
    }
}
"""


def _source_evidence(source, *, start=1, path="service/src/main/java/com/sample/orders/legacy/LegacyCodeMapper.java", revision="54d73240d6b5"):
    iid = uuid4()
    eid = uuid4()
    now = datetime.now(UTC)
    lines = "\n".join(f"{start + i}| {ln}" for i, ln in enumerate(source.splitlines()))
    end = start + max(0, len(source.splitlines()) - 1)
    return Evidence(
        evidence_id=eid,
        tenant_id="tenant-a",
        investigation_id=iid,
        evidence_type=EvidenceType.SOURCE_CODE,
        provider="pygit2-local",
        source=f"git://repo/{path}@{revision}",
        title="Java source range",
        summary="Pinned Java source",
        content_snippet=lines,
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
                line_start=start,
                line_end=end,
                revision=revision,
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=str(eid),
    )


def test_resolves_l5_null_guard_return():
    ev = _source_evidence(_MAPPER)
    result = resolve_java_method_return_fact(
        ev,
        "normalize",
        guard_condition="orderType == null",
        declaring_type="LegacyCodeMapper",
    )
    assert result.status == CodeFactStatus.RESOLVED
    assert result.resolved_value == "STANDARD"
    assert result.fact_category == JavaFactCategory.SOURCE_VALUE_FACT
    assert result.value_semantics == CodeValueSemantics.GUARDED_RETURN_LITERAL
    assert (
        result.subject_identity
        == "com.sample.orders.legacy.LegacyCodeMapper.normalize"
    )
    assert result.declaration_location.line_start == 5
    assert result.declaration_location.line_end == 5
    assert result.source_evidence_ids == (ev.evidence_id,)
    assert result.revision == "54d73240d6b5"


def test_resolves_without_declaring_type():
    ev = _source_evidence(_MAPPER)
    result = resolve_java_method_return_fact(ev, "normalize", guard_condition="orderType == null")
    assert result.status == CodeFactStatus.RESOLVED
    assert result.resolved_value == "STANDARD"


_LIVE_L5_WINDOW = """    public String normalize(String orderType) {
        if (orderType == null) {
            return "STANDARD";
        }
        switch (orderType) {
            case "LEGACY-BULK":
                return "BULK";
            case "LEGACY-RES":
                return "RESIDENTIAL";
            default:
                return orderType;
        }
    }
}"""


def test_live_l5_window_with_class_brace_resolves():
    """Live 041339 L5#1: GET_CODE banks 6-19 (method 6-18 + class `}`)."""
    source = _LIVE_L5_WINDOW
    start = 6
    lines = "\n".join(f"{start + i}| {ln}" for i, ln in enumerate(source.splitlines()))
    ev = _source_evidence(source, start=start)
    assert ev.content_snippet == lines
    result = resolve_java_method_return_fact(
        ev,
        "normalize",
        guard_condition="orderType == null",
        declaring_type="LegacyCodeMapper",
    )
    assert result.status == CodeFactStatus.RESOLVED
    assert result.resolved_value == "STANDARD"
    assert result.declaration_location.line_start == 8
    assert result.declaration_location.line_end == 8


def test_method_only_window_still_resolves():
    """Narrower 6-18 method window keeps resolving (no behavior change)."""
    source = "\n".join(_LIVE_L5_WINDOW.splitlines()[:-1])
    ev = _source_evidence(source, start=6)
    result = resolve_java_method_return_fact(
        ev,
        "normalize",
        guard_condition="orderType == null",
        declaring_type="LegacyCodeMapper",
    )
    assert result.status == CodeFactStatus.RESOLVED
    assert result.resolved_value == "STANDARD"


def test_garbage_window_stays_unresolved():
    """A genuinely broken window is not rescued by brace stripping."""
    ev = _source_evidence("public String normalize( { if ( ", start=6)
    result = resolve_java_method_return_fact(
        ev,
        "normalize",
        guard_condition="orderType == null",
        declaring_type="LegacyCodeMapper",
    )
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "java_syntax_error_or_incomplete_source_window" in result.limitation_codes


def test_wrong_guard_is_unresolved():
    ev = _source_evidence(_MAPPER)
    result = resolve_java_method_return_fact(
        ev, "normalize", guard_condition="orderType == null and x"
    )
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "guard_return_not_found" in result.limitation_codes


def test_unknown_method_is_unresolved():
    ev = _source_evidence(_MAPPER)
    result = resolve_java_method_return_fact(ev, "denormalize", guard_condition="orderType == null")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "method_not_found" in result.limitation_codes


def test_non_literal_return_is_unresolved():
    source = _MAPPER.replace('return "STANDARD";', "return compute(orderType);")
    ev = _source_evidence(source)
    result = resolve_java_method_return_fact(ev, "normalize", guard_condition="orderType == null")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "guard_return_not_found" in result.limitation_codes


def test_unpinned_revision_is_unresolved():
    ev = _source_evidence(_MAPPER, revision="HEAD")
    result = resolve_java_method_return_fact(ev, "normalize", guard_condition="orderType == null")
    assert result.status == CodeFactStatus.UNRESOLVED
    # Fail-closed on provenance: java_source_lines rejects HEAD before the
    # resolver runs; a pinned-but-synthetic HEAD-shaped row asserts nothing.
    assert result.limitation_codes


def test_non_java_file_is_unresolved():
    ev = _source_evidence(_MAPPER, path="service/normalize.py")
    result = resolve_java_method_return_fact(ev, "normalize")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "not_a_java_source_file" in result.limitation_codes


def test_l5_claim_verifies_established_end_to_end():
    ev = _source_evidence(_MAPPER)
    resolution = resolve_java_method_return_fact(
        ev,
        "normalize",
        guard_condition="orderType == null",
        declaring_type="LegacyCodeMapper",
    )
    assert resolution.status == CodeFactStatus.RESOLVED
    banked = ev.model_copy(
        update={
            "attributes": {
                **ev.attributes,
                CODE_FACT_RESOLUTION_V2_KEY: resolution.model_dump(mode="json"),
            }
        }
    )
    hyp = SimpleNamespace(
        id=uuid4(),
        statement=(
            "If orderType is null, LegacyCodeMapper.normalize returns 'STANDARD', "
            "so the default order type applies"
        ),
        claimed_value="STANDARD",
        claim_target=SimpleNamespace(
            symbol="com.sample.orders.legacy.LegacyCodeMapper.normalize",
            declaring_type="com.sample.orders.legacy.LegacyCodeMapper",
            claimed_value="STANDARD",
        ),
        fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
        supporting_evidence_ids=[banked.evidence_id],
    )
    ok, _ = VerificationEngine.java_fact_proof(hyp, [banked])
    assert ok
    result = VerificationEngine().validate_java_fact_claim(hyp, [banked], [])
    assert result is not None and result.tier == "established"
    assert result.status.value == "VERIFIED"


def test_conflicting_claimed_value_fails_closed():
    ev = _source_evidence(_MAPPER)
    resolution = resolve_java_method_return_fact(
        ev, "normalize", guard_condition="orderType == null"
    )
    banked = ev.model_copy(
        update={
            "attributes": {
                **ev.attributes,
                CODE_FACT_RESOLUTION_V2_KEY: resolution.model_dump(mode="json"),
            }
        }
    )
    hyp = SimpleNamespace(
        id=uuid4(),
        statement="normalize returns 'EXPRESS' on null",
        claimed_value="EXPRESS",
        claim_target=SimpleNamespace(
            symbol="com.sample.orders.legacy.LegacyCodeMapper.normalize",
            claimed_value="EXPRESS",
        ),
        fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
        supporting_evidence_ids=[banked.evidence_id],
    )
    ok, _ = VerificationEngine.java_fact_proof(hyp, [banked])
    assert not ok
    assert VerificationEngine().validate_java_fact_claim(hyp, [banked], []) is None


def test_method_return_fact_without_structured_value_fails_closed():
    ev = _source_evidence(_MAPPER)
    resolution = resolve_java_method_return_fact(ev, "normalize", guard_condition="orderType == null")
    banked = ev.model_copy(
        update={
            "attributes": {
                **ev.attributes,
                CODE_FACT_RESOLUTION_V2_KEY: resolution.model_dump(mode="json"),
            }
        }
    )
    hyp = SimpleNamespace(
        id=uuid4(),
        statement="When orderType is null, normalize returns STANDARD",
        claim_target=SimpleNamespace(
            symbol="com.sample.orders.legacy.LegacyCodeMapper.normalize",
            claimed_value=None,
        ),
        fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
        supporting_evidence_ids=[banked.evidence_id],
    )
    ok, reason = VerificationEngine.java_fact_proof(hyp, [banked])
    assert not ok
    assert "no structured claimed_value" in reason
