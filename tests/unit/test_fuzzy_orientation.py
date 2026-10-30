# tests/unit/test_fuzzy_orientation.py
"""Step 5 (D4): frozen FuzzyOrientation, reason-context steering, validator enforcement.

Orientation is direction only: it steers which deterministic reads fire next
and can never be cited, banked, or concluded from. Any claim whose only
support traces to orientation must fail validation.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from investigation_agent_platform.application.evidence.code_recall import (
    FUZZY_ORIENTATION_ATTR,
    FuzzyOrientation,
    build_fuzzy_orientation,
    is_orientation_traced_evidence,
)
from investigation_agent_platform.application.investigation.reasoning import (
    ReasoningCoordinator,
    fuzzy_orientation_block,
)
from investigation_agent_platform.application.investigation.verification import (
    CausalRelationship,
    RootCauseVerificationPolicy,
    VerificationEngine,
    VerificationStatus,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.hypothesis.models import Hypothesis, HypothesisStatus
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)


def _orientation(**over: object) -> FuzzyOrientation:
    kwargs: dict[str, object] = {
        "miss": "ZERO_EXACT_HITS",
        "suggested_symbols": ["svc::Handler::charge"],
        "steering_note": "read the handler",
    }
    kwargs.update(over)
    return FuzzyOrientation(**kwargs)  # type: ignore[arg-type]


def test_orientation_is_frozen_and_closed() -> None:
    orientation = _orientation()
    assert orientation.unverified is True
    with pytest.raises(ValidationError):
        orientation.unverified = False  # type: ignore[misc]
    with pytest.raises(ValidationError):
        FuzzyOrientation(miss="x", suggested_symbols=[], bogus_field="y")  # type: ignore[call-arg]


def test_orientation_bounds_lists_and_text() -> None:
    with pytest.raises(ValidationError):
        _orientation(suggested_symbols=[f"s{i}" for i in range(11)])
    with pytest.raises(ValidationError):
        _orientation(steering_note="n" * 513)


def test_build_orientation_takes_symbols_from_authority_only() -> None:
    nodes = [
        SimpleNamespace(qualified_name="svc::A::one"),
        {"qualified_name": "svc::B::two"},
        SimpleNamespace(qualified_name=""),
        SimpleNamespace(),
    ]
    orientation = build_fuzzy_orientation("AMBIGUOUS_SYMBOL", nodes, steering_note="s")
    assert orientation.miss == "AMBIGUOUS_SYMBOL"
    assert orientation.suggested_symbols == ["svc::A::one", "svc::B::two"]
    assert orientation.unverified is True


def test_orientation_marker_detection() -> None:
    assert (
        is_orientation_traced_evidence(SimpleNamespace(attributes={FUZZY_ORIENTATION_ATTR: True}))
        is True
    )
    assert (
        is_orientation_traced_evidence(SimpleNamespace(attributes={FUZZY_ORIENTATION_ATTR: False}))
        is False
    )
    assert is_orientation_traced_evidence(SimpleNamespace(attributes={})) is False
    assert is_orientation_traced_evidence(SimpleNamespace()) is False
    assert is_orientation_traced_evidence(object()) is False
    # Documentation rows stay citable for doc answers: only the marker counts.
    assert is_orientation_traced_evidence(SimpleNamespace(attributes={"source": "refdoc"})) is False


def test_fuzzy_orientation_block_serialization() -> None:
    assert fuzzy_orientation_block(None) is None
    assert fuzzy_orientation_block({}) is None
    assert fuzzy_orientation_block("hint") is None
    block = fuzzy_orientation_block(_orientation())
    assert block is not None
    assert block["type"] == "fuzzy_orientation"
    assert block["unverified"] is True
    assert block["miss"] == "ZERO_EXACT_HITS"
    assert block["suggested_symbols"] == ["svc::Handler::charge"]
    assert block["steering_note"] == "read the handler"
    assert "evidence_ids" not in block
    from_dict = fuzzy_orientation_block({"miss": "AMBIGUOUS_SYMBOL", "suggested_symbols": ["a", 1]})
    assert from_dict is not None
    assert from_dict["unverified"] is True
    assert from_dict["suggested_symbols"] == ["a", "1"]


def _ev(
    etype: EvidenceType = EvidenceType.SOURCE_CODE,
    *,
    orientation_traced: bool = False,
) -> Evidence:
    iid = uuid.uuid4()
    now = datetime.now(UTC)
    attributes = {FUZZY_ORIENTATION_ATTR: True} if orientation_traced else {}
    return Evidence(
        tenant_id="tenant-a",
        investigation_id=iid,
        evidence_type=etype,
        provider="prov-a",
        source="urn:src",
        title="t",
        summary="s",
        fingerprint=f"fp-{uuid.uuid4()}",
        observed_at=now,
        retrieved_at=now,
        attributes=attributes,
        provenance=EvidenceProvenance(
            tenant_id="tenant-a",
            investigation_id=iid,
            provider_type="test",
            requested_provider_id="test",
            actual_provider_id="test",
            source_system="test",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="test", operation="test", normalized_query_hash="abc"
            ),
            source_location=SourceLocation(system="test", identifier="x"),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
    )


def _hyp(statement: str = "the handler writes the file") -> Hypothesis:
    iid = uuid.uuid4()
    return Hypothesis(
        tenant_id="tenant-a",
        investigation_id=iid,
        title=statement[:256],
        description=statement,
        statement=statement,
        status=HypothesisStatus.PROPOSED,
    )


def _chain(*evidence_ids: uuid.UUID) -> list[CausalRelationship]:
    return [
        CausalRelationship(
            cause="request arrives",
            mechanism="handler writes the file",
            effect="file updated",
            evidence_ids=list(evidence_ids),
            confidence=0.5,
        )
    ]


def test_orientation_only_claim_never_verifies() -> None:
    engine = VerificationEngine()
    marker = _ev(EvidenceType.LOG, orientation_traced=True)
    hypothesis = _hyp().model_copy(update={"supporting_evidence_ids": [marker.evidence_id]})
    result = engine.evaluate_hypothesis(
        hypothesis,
        [marker],
        [],
        _chain(marker.evidence_id),
        RootCauseVerificationPolicy(),
    )
    assert result.status == VerificationStatus.INCONCLUSIVE
    assert result.status != VerificationStatus.VERIFIED
    assert "orientation" in result.explanation.lower()
    assert result.corroborating_evidence_ids == []


def test_orientation_only_via_causal_steps_alone_never_verifies() -> None:
    engine = VerificationEngine()
    marker = _ev(EvidenceType.SOURCE_CODE, orientation_traced=True)
    result = engine.evaluate_hypothesis(
        _hyp(),
        [marker],
        [],
        _chain(marker.evidence_id),
        RootCauseVerificationPolicy(),
    )
    assert result.status == VerificationStatus.INCONCLUSIVE
    assert "orientation" in result.explanation.lower()


def test_grounding_row_beside_orientation_passes_the_gate() -> None:
    real = _ev(EvidenceType.SOURCE_CODE)
    marker = _ev(EvidenceType.LOG, orientation_traced=True)
    hypothesis = _hyp().model_copy(
        update={"supporting_evidence_ids": [real.evidence_id, marker.evidence_id]}
    )
    supported, _ = VerificationEngine.cites_only_orientation(
        hypothesis, [real, marker], _chain(real.evidence_id)
    )
    assert supported is False


def test_unknown_ids_alone_are_not_orientation_only() -> None:
    supported, _ = VerificationEngine.cites_only_orientation(_hyp(), [_ev()], _chain(uuid.uuid4()))
    assert supported is False


def test_uncited_claim_is_not_orientation_only() -> None:
    supported, _ = VerificationEngine.cites_only_orientation(_hyp(), [_ev()], [])
    assert supported is False


def test_step_citing_only_orientation_marker_is_ungrounded() -> None:
    # A LOG-typed row would normally ground a step; the marker vetoes it.
    marker = _ev(EvidenceType.LOG, orientation_traced=True)
    grounded, reason = VerificationEngine.causal_step_grounding(
        _chain(marker.evidence_id), [marker]
    )
    assert grounded is False
    assert "orientation" in reason.lower()


def test_step_citing_unmarked_row_still_grounds() -> None:
    row = _ev(EvidenceType.LOG)
    grounded, _ = VerificationEngine.causal_step_grounding(_chain(row.evidence_id), [row])
    assert grounded is True


def _state(problem: str = "where is auth handled?"):
    from datetime import timedelta

    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
        InvestigationState,
    )

    now = datetime.now(UTC)
    inv = Investigation(
        session_id="sess-1",
        application_id="app",
        tenant_id="tenant-a",
        request=InvestigationRequest(
            application_id="app",
            problem_description=problem,
            session_id="sess-1",
            requested_by="tester",
        ),
        context=InvestigationContext(
            environment="production",
            time_window=(now - timedelta(seconds=3600), now),
        ),
    )
    return InvestigationState(investigation=inv)


def _prompt_for(problem: str, fuzzy_orientation: object) -> dict:
    import asyncio as _asyncio

    from investigation_agent_platform.ports.observability.telemetry import (
        LLMCallMetadata,
    )
    from investigation_agent_platform.ports.reasoning.llm_gateway import (
        LLMGatewayResponse,
    )

    safety = MagicMock()
    safety.validate_prompt_safety = AsyncMock(return_value=True)
    gateway = MagicMock()
    gateway.complete = AsyncMock(
        return_value=LLMGatewayResponse(
            content="{}",
            parsed={"observations": [], "conclusion_readiness": 0.0},
            metadata=LLMCallMetadata(
                model_name="test",
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
                estimated_cost_usd=0.0,
                latency_ms=1.0,
            ),
        )
    )
    coord = ReasoningCoordinator(prompt_safety_policy=safety, llm_gateway=gateway)
    _asyncio.run(coord.reason("tenant-a", _state(problem), fuzzy_orientation=fuzzy_orientation))
    call = gateway.complete.call_args
    request = call.kwargs.get("request") if call.kwargs else None
    if request is None and call.args:
        request = call.args[-1]
    prompt = getattr(request, "prompt", "") or ""
    payload = prompt.split("STRUCTURED_CONTEXT_JSON=", 1)[1]
    return json.loads(payload)


def test_reason_context_carries_fuzzy_orientation() -> None:
    facts = _prompt_for("charge", _orientation())
    block = facts["fuzzy_orientation"]
    assert block["type"] == "fuzzy_orientation"
    assert block["unverified"] is True
    assert block["miss"] == "ZERO_EXACT_HITS"
    assert block["suggested_symbols"] == ["svc::Handler::charge"]
    assert "evidence_ids" not in block


def test_reason_context_omits_fuzzy_orientation_when_absent() -> None:
    facts = _prompt_for("charge", None)
    assert "fuzzy_orientation" not in facts
