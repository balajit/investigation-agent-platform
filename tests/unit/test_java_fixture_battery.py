"""Phase 8 manifest-driven fixture battery with verifier spot-checks."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from investigation_agent_platform.application.analysis.java import (
    boundary_facts,
    dataflow,
    field_facts,
    flow_facts,
    init_facts,
    local_facts,
    method_facts,
    type_facts,
)
from investigation_agent_platform.application.investigation.verification import (
    VerificationEngine,
)
from investigation_agent_platform.domain.code.models import CodeValueSemantics
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "java"
_MANIFEST = json.loads((_FIXTURES / "manifest.json").read_text())


def _evidence_for(filename: str) -> Evidence:
    source = (_FIXTURES / filename).read_text()
    start = 10
    investigation_id = uuid4()
    evidence_id = uuid4()
    now = datetime.now(UTC)
    lines = "\n".join(f"{start + index}| {line}" for index, line in enumerate(source.splitlines()))
    end = start + max(0, len(source.splitlines()) - 1)
    return Evidence(
        evidence_id=evidence_id,
        tenant_id="tenant-a",
        investigation_id=investigation_id,
        evidence_type=EvidenceType.SOURCE_CODE,
        provider="pygit2-local",
        source=f"git://repo/{filename}@abc123",
        title="Java fixture range",
        summary="Pinned Java fixture",
        content_snippet=lines,
        attributes={"read_mode": "range"},
        observed_at=now,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id="tenant-a",
            investigation_id=investigation_id,
            provider_type="GIT",
            requested_provider_id="pygit2-local",
            actual_provider_id="pygit2-local",
            source_system="Git",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="GIT", operation="GET_SOURCE", normalized_query_hash="hash"
            ),
            source_location=SourceLocation(
                system="Git",
                identifier=f"repo/{filename}",
                file_path=filename,
                line_start=start,
                line_end=end,
                revision="abc123",
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=str(evidence_id),
    )


def _resolve(case: dict) -> object:
    evidence = _evidence_for(case["file"])
    kwargs = dict(case.get("kwargs", {}))
    level = case.get("language_version")
    if level is not None:
        kwargs["language_version"] = level
    resolver = case["resolver"]
    symbol = case["symbol"]
    if resolver == "field":
        return field_facts.resolve_java_field_fact(evidence, symbol, **kwargs)
    if resolver == "local":
        return local_facts.resolve_java_local_fact(evidence, symbol, **kwargs)
    if resolver == "init":
        return init_facts.resolve_java_init_fact(evidence, symbol, **kwargs)
    if resolver == "call":
        return method_facts.resolve_java_call_target(evidence, symbol, **kwargs)
    if resolver == "dataflow":
        return dataflow.resolve_java_data_flow(evidence, symbol, **kwargs)
    if resolver == "control":
        return flow_facts.resolve_java_control_flow(evidence, symbol, **kwargs)
    if resolver == "type":
        return type_facts.resolve_java_type_fact(evidence, symbol, **kwargs)
    if resolver == "boundary":
        return boundary_facts.resolve_java_framework_boundary(evidence, symbol, **kwargs)
    raise AssertionError(f"unknown resolver {resolver!r}")


@pytest.mark.parametrize("case", _MANIFEST["cases"], ids=lambda c: f"{c['file']}::{c['symbol']}")
def test_fixture_battery(case):
    result = _resolve(case)
    assert result.status.value == case["expect_status"], (
        f"{case['file']}::{case['symbol']}: {result.status} {result.limitation_codes}"
    )
    for code in case.get("expect_codes", []):
        assert code in result.limitation_codes
    if "expect_value" in case:
        assert result.resolved_value == case["expect_value"]
    if "expect_target" in case:
        assert result.call_target == case["expect_target"]
    if "expect_fact" in case:
        assert result.fact_category.value == case["expect_fact"]


def test_battery_never_claims_runtime_effective():
    for case in _MANIFEST["cases"]:
        result = _resolve(case)
        assert result.value_semantics != CodeValueSemantics.RUNTIME_EFFECTIVE
        stage = getattr(result, "semantic_stage", None)
        if stage is not None:
            assert stage.value != "RUNTIME_EFFECTIVE"


def test_battery_resolved_field_claim_proves_and_tampered_fails():
    evidence = _evidence_for("FieldDefaults.java")
    dumped = field_facts.resolve_java_field_fact(
        evidence, "retryCount", declaring_type="FieldDefaults"
    ).model_dump(mode="json")
    banked = evidence.model_copy(
        update={"attributes": {**evidence.attributes, "code_fact_resolution": dumped}}
    )
    hypothesis = SimpleNamespace(
        id=uuid4(),
        statement="The default value of retryCount is 3",
        claim_value_semantics="EXPLICIT_INITIALIZER",
        claimed_value="3",
        claim_target=SimpleNamespace(
            symbol="retryCount",
            declaring_type="com.example.battery.FieldDefaults",
            file_path="FieldDefaults.java",
            line_start=11,
        ),
        supporting_evidence_ids=[banked.evidence_id],
    )
    ok, reason = VerificationEngine.code_value_proof(hypothesis, [banked])
    assert ok, reason
    tampered = SimpleNamespace(
        id=uuid4(),
        statement="The default value of retryCount is 4",
        claim_value_semantics="EXPLICIT_INITIALIZER",
        claimed_value="4",
        claim_target=hypothesis.claim_target,
        supporting_evidence_ids=[banked.evidence_id],
    )
    ok, _ = VerificationEngine.code_value_proof(tampered, [banked])
    assert not ok
