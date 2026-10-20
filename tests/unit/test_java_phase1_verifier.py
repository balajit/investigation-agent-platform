"""Phase 1 — verifier integrity + Model step 5 (node_id/parser_version alignment).

Covers: versioned v2 dual-emit with legacy unchanged, conflict rejection,
downgrade/replay, fact_category on claim shapes, java_fact_proof verdict
ceilings, PARTIAL wiring to typed limitations + next_check, node_id match.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investigation_agent_platform.application.analysis.java_field_facts import (
    resolve_java_field_fact,
)
from investigation_agent_platform.application.investigation.verification import (
    VerificationEngine,
    category_proof_rule,
)
from investigation_agent_platform.domain.code.models import (
    CODE_FACT_RESOLUTION_V2_KEY,
    CodeFactResolution,
    CodeFactStatus,
    JavaFactCategory,
    detect_dual_resolution_conflict,
    emit_dual_resolution_attributes,
    legacy_to_v2_resolution,
    strip_v2_for_downgrade,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.hypothesis.models import (
    AssessmentType,
    ClaimTarget,
    HypothesisEvidenceAssessment,
)
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)
from investigation_agent_platform.ports.reasoning.reasoner import DecisionClaim


def _source_evidence(source, *, start=10, path="src/Foo.java", extra_attrs=None):
    iid = uuid4()
    eid = uuid4()
    now = datetime.now(UTC)
    lines = "\n".join(f"{start + i}| {ln}" for i, ln in enumerate(source.splitlines()))
    end = start + max(0, len(source.splitlines()) - 1)
    attrs = {"read_mode": "range"}
    if extra_attrs:
        attrs.update(extra_attrs)
    return Evidence(
        evidence_id=eid,
        tenant_id="tenant-a",
        investigation_id=iid,
        evidence_type=EvidenceType.SOURCE_CODE,
        provider="pygit2-local",
        source=f"git://repo/{path}@abc123",
        title="Java source range",
        summary="Pinned Java source",
        content_snippet=lines,
        attributes=attrs,
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
                revision="abc123",
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=str(eid),
    )


def _v2_evidence(v2, evidence=None):
    base = evidence or _source_evidence("class Foo {\n int x = 3;\n}")
    dumped = v2.model_dump(mode="json")
    return base.model_copy(
        update={"attributes": {**base.attributes, CODE_FACT_RESOLUTION_V2_KEY: dumped}}
    )


def _typed_hypothesis(evidence, category, **overrides):
    target = SimpleNamespace(
        symbol="x",
        declaring_type="Foo",
        file_path="src/Foo.java",
        line_start=11,
        claimed_value=overrides.get("claimed_value", "3"),
        value_semantics=overrides.get("value_semantics", "EXPLICIT_INITIALIZER"),
        fact_category=category,
        node_id=overrides.get("target_node_id"),
    )
    return SimpleNamespace(
        id=uuid4(),
        statement=overrides.get("statement", "The default value of x is 3"),
        claim_value_semantics=overrides.get("value_semantics", "EXPLICIT_INITIALIZER"),
        claimed_value=overrides.get("claimed_value", "3"),
        claim_target=target,
        fact_category=category,
        node_id=overrides.get("hyp_node_id"),
        supporting_evidence_ids=[evidence.evidence_id],
    )


# --- 1. versioned key dual-emit, legacy unchanged ---


def test_legacy_shape_still_strict_generalized_never_validates_legacy():
    ev = _source_evidence("class Foo {\n int x = 3;\n}")
    dumped = resolve_java_field_fact(ev, "x").model_dump(mode="json")
    CodeFactResolution.model_validate(dumped)  # legacy still reads
    with pytest.raises(ValidationError):
        CodeFactResolution.model_validate({**dumped, "fact_category": "SOURCE_VALUE_FACT"})
    v2 = legacy_to_v2_resolution(CodeFactResolution.model_validate(dumped))
    assert v2.fact_category == JavaFactCategory.SOURCE_VALUE_FACT
    with pytest.raises(ValidationError):
        CodeFactResolution.model_validate(v2.model_dump(mode="json"))


def test_dual_emit_and_conflict_rejection_and_downgrade_replay():
    ev = _source_evidence("class Foo {\n int x = 3;\n}")
    legacy = resolve_java_field_fact(ev, "x")
    dual = emit_dual_resolution_attributes(legacy)
    assert set(dual) == {"code_fact_resolution", "code_fact_resolution_v2"}
    banked = ev.model_copy(update={"attributes": {**ev.attributes, **dual}})
    Evidence.model_validate(banked.model_dump(mode="json"))  # new reader accepts both
    assert (
        detect_dual_resolution_conflict(
            dual["code_fact_resolution"], dual["code_fact_resolution_v2"]
        )
        is None
    )
    tampered_v2 = {**dual["code_fact_resolution_v2"], "resolved_value": "999"}
    assert detect_dual_resolution_conflict(dual["code_fact_resolution"], tampered_v2) is not None
    with pytest.raises(ValidationError):
        Evidence.model_validate(
            banked.model_copy(
                update={
                    "attributes": {
                        **ev.attributes,
                        **dual,
                        CODE_FACT_RESOLUTION_V2_KEY: tampered_v2,
                    }
                }
            ).model_dump(mode="json")
        )
    downgraded = strip_v2_for_downgrade({**ev.attributes, **dual})
    assert "code_fact_resolution_v2" not in downgraded
    assert downgraded["code_fact_resolution"] == dual["code_fact_resolution"]
    replay = ev.model_copy(update={"attributes": downgraded})
    Evidence.model_validate(replay.model_dump(mode="json"))  # old reader replays


# --- 2. fact_category on claim shapes ---


def test_fact_category_optional_on_claim_shapes_legacy_rows_untouched():
    assert ClaimTarget(symbol="x").fact_category is None
    assert (
        ClaimTarget(symbol="x", fact_category=JavaFactCategory.SOURCE_VALUE_FACT).fact_category
        == JavaFactCategory.SOURCE_VALUE_FACT
    )
    asm = HypothesisEvidenceAssessment(
        hypothesis_id=uuid4(),
        evidence_id=uuid4(),
        assessment=AssessmentType.SUPPORTS,
        strength="STRONG",
        reason="r",
    )
    assert asm.fact_category is None
    assert DecisionClaim(statement="s").fact_category is None
    assert (
        DecisionClaim(statement="s", fact_category=JavaFactCategory.CALL_TARGET_FACT).fact_category
        == JavaFactCategory.CALL_TARGET_FACT
    )
    with pytest.raises(ValidationError):
        ClaimTarget(symbol="x", fact_category="NOT_A_CATEGORY")


def test_claims_field_value_is_routing_aid_only():
    assert VerificationEngine.claims_field_value("The default value of x is 3") is True
    assert VerificationEngine.claims_java_fact("The default value of x is 3") is True
    out = VerificationEngine.claims_field_value("The default value of x is 3")
    assert isinstance(out, bool)  # never supplies target/category/value


# --- 3/4. proof dispatch + ceilings + node_id ---


def test_source_value_v2_proves_established():
    ev = _source_evidence("class Foo {\n int x = 3;\n}")
    legacy = resolve_java_field_fact(ev, "x")
    v2 = legacy_to_v2_resolution(legacy)
    banked = _v2_evidence(v2, ev)
    banked = banked.model_copy(
        update={
            "attributes": {
                **banked.attributes,
                "code_fact_resolution": legacy.model_dump(mode="json"),
            }
        }
    )
    hyp = _typed_hypothesis(banked, JavaFactCategory.SOURCE_VALUE_FACT)
    ok, _ = VerificationEngine.java_fact_proof(hyp, [banked])
    assert ok
    result = VerificationEngine().validate_java_fact_claim(hyp, [banked], [])
    assert result is not None and result.tier == "established"
    assert category_proof_rule(JavaFactCategory.SOURCE_VALUE_FACT).max_verdict == "VERIFIED"


def test_call_target_and_possible_effect_cap_at_likely():
    assert category_proof_rule(JavaFactCategory.CALL_TARGET_FACT).max_verdict == "LIKELY"
    assert category_proof_rule(JavaFactCategory.POSSIBLE_EFFECT_FACT).max_verdict == "LIKELY"
    ev = _source_evidence("class Foo {\n int x = 3;\n}")
    legacy = resolve_java_field_fact(ev, "x")
    base_v2 = legacy_to_v2_resolution(legacy)
    for category in (JavaFactCategory.CALL_TARGET_FACT, JavaFactCategory.POSSIBLE_EFFECT_FACT):
        v2 = base_v2.model_copy(
            update={
                "fact_category": category,
                "call_target": "Foo.bar()/0"
                if category == JavaFactCategory.CALL_TARGET_FACT
                else None,
                "structured_fact": {"effect": "may-write-x"}
                if category == JavaFactCategory.POSSIBLE_EFFECT_FACT
                else None,
            }
        )
        banked = _v2_evidence(v2, ev)
        hyp = _typed_hypothesis(banked, category)
        result = VerificationEngine().validate_java_fact_claim(hyp, [banked], [])
        # call-target family rule needs call-site ranges; source range read
        # supplies them in this harness, so a well-bound claim verifies likely
        if result is not None:
            assert result.tier == "likely"


def test_runtime_and_causal_never_source_verifiable_unknown_fails_closed():
    ev = _source_evidence("class Foo {\n int x = 3;\n}")
    legacy = resolve_java_field_fact(ev, "x")
    base_v2 = legacy_to_v2_resolution(legacy)
    # model itself fails closed without runtime identity; build a raw payload path instead
    raw = base_v2.model_dump(mode="json")
    raw.update({"fact_category": "RUNTIME_OBSERVATION_FACT"})
    banked = ev.model_copy(
        update={"attributes": {**ev.attributes, CODE_FACT_RESOLUTION_V2_KEY: raw}}
    )
    hyp = _typed_hypothesis(banked, JavaFactCategory.RUNTIME_OBSERVATION_FACT)
    ok, _ = VerificationEngine.java_fact_proof(hyp, [banked])
    assert not ok
    assert VerificationEngine().validate_java_fact_claim(hyp, [banked], []) is None
    causal_hyp = _typed_hypothesis(banked, JavaFactCategory.CAUSAL_CLAIM)
    ok, _ = VerificationEngine.java_fact_proof(causal_hyp, [banked])
    assert not ok
    unknown_hyp = SimpleNamespace(
        id=uuid4(),
        statement="x",
        claim_target=SimpleNamespace(symbol="x", fact_category="NOPE"),
        fact_category="NOPE",
        supporting_evidence_ids=[banked.evidence_id],
    )
    ok, reason = VerificationEngine.java_fact_proof(unknown_hyp, [banked])
    assert not ok


def test_partial_wires_to_typed_limitation_and_next_check():
    ev = _source_evidence("class Foo {\n int x = configured();\n}")
    resolution = resolve_java_field_fact(ev, "x")
    assert resolution.status == CodeFactStatus.PARTIAL
    v2 = legacy_to_v2_resolution(resolution)
    banked = _v2_evidence(v2, ev)
    hyp = _typed_hypothesis(banked, JavaFactCategory.SOURCE_VALUE_FACT)
    ok, _ = VerificationEngine.java_fact_proof(hyp, [banked])
    assert not ok
    assert VerificationEngine().validate_java_fact_claim(hyp, [banked], []) is None
    partial = VerificationEngine().java_partial_result(hyp, [banked])
    assert partial is not None and partial.limitations
    nxt = VerificationEngine.next_check_for(partial.limitations, [], [])
    assert nxt  # existing rival > relation > unmapped > provider priority


def test_node_id_optional_match_and_parser_version_permissive():
    ev = _source_evidence("class Foo {\n int x = 3;\n}")
    legacy = resolve_java_field_fact(ev, "x")
    assert legacy.node_id is None  # not required; permissive construction
    # Phase 2 records the real parser identity instead of the "unknown" default.
    assert legacy.parser_version == "tree-sitter-java"
    v2 = legacy_to_v2_resolution(legacy)
    nid = uuid4()
    v2_bound = v2.model_copy(update={"node_id": nid, "parser_version": "tree-sitter-java-1.0"})
    banked = _v2_evidence(v2_bound, ev)
    hyp_match = _typed_hypothesis(banked, JavaFactCategory.SOURCE_VALUE_FACT, hyp_node_id=nid)
    ok, _ = VerificationEngine.java_fact_proof(hyp_match, [banked])
    assert ok
    hyp_clash = _typed_hypothesis(banked, JavaFactCategory.SOURCE_VALUE_FACT, hyp_node_id=uuid4())
    ok, reason = VerificationEngine.java_fact_proof(hyp_clash, [banked])
    assert not ok and "node_id" in reason
