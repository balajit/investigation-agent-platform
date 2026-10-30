# tests/unit/test_verify_outcome.py
"""Phase 1 determinism: VERIFY verdicts persist scores, bindings, lifecycle."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from investigation_agent_platform.api.dependencies import (
    AppContext,
    set_app_context,
)
from investigation_agent_platform.application.investigation.verification import (
    InvestigationConfidence,
    VerificationResult,
    VerificationStatus,
    apply_verification_outcome,
)
from investigation_agent_platform.application.worker.activities import (
    VerifyRootCauseInput,
    _latest_fuzzy_orientation,
    verify_root_cause_activity,
)
from investigation_agent_platform.domain.hypothesis.models import (
    Hypothesis,
    HypothesisStatus,
)
from investigation_agent_platform.domain.investigation.models import (
    ActionOutcome,
    ActionType,
)


def _hyp(**over):
    base = {
        "tenant_id": "tenant-a",
        "investigation_id": uuid4(),
        "title": "t",
        "statement": "s",
        "description": "d",
    }
    base.update(over)
    return Hypothesis(**base)


def _conf(coverage=0.6, reliability=0.7, causal=0.5, penalty=0.0):
    return InvestigationConfidence(
        coverage_score=coverage,
        reliability_score=reliability,
        causal_score=causal,
        contradiction_penalty=penalty,
    )


def _result(status, corroborating=(), against=(), conf=None):
    return VerificationResult(
        hypothesis_id=uuid4(),
        status=status,
        corroborating_evidence_ids=list(corroborating),
        explanation="e",
        confidence=conf or _conf(),
        against_evidence=list(against),
    )


def test_verified_persists_scores_bindings_and_supported():
    cited = [uuid4(), uuid4()]
    out = apply_verification_outcome(
        _hyp(), _result(VerificationStatus.VERIFIED, corroborating=cited)
    )
    assert out.status == HypothesisStatus.SUPPORTED
    assert out.support_score == 0.6
    assert out.confidence_score == _conf().overall_confidence
    assert out.supporting_evidence_ids == cited


def test_inconclusive_advances_proposed_and_writes_scores():
    out = apply_verification_outcome(_hyp(), _result(VerificationStatus.INCONCLUSIVE))
    assert out.status == HypothesisStatus.UNDER_INVESTIGATION
    assert out.support_score == 0.6
    assert out.refuting_evidence_ids == []


def test_contradicted_chains_through_under_investigation():
    bad = uuid4()
    out = apply_verification_outcome(
        _hyp(), _result(VerificationStatus.CONTRADICTED, against=[str(bad), "not-a-uuid"])
    )
    assert out.status == HypothesisStatus.CONTRADICTED
    assert out.refuting_evidence_ids == [bad]


def test_supported_candidate_contradicted_directly():
    out = apply_verification_outcome(
        _hyp(status=HypothesisStatus.SUPPORTED), _result(VerificationStatus.CONTRADICTED)
    )
    assert out.status == HypothesisStatus.CONTRADICTED


def test_rejected_candidate_never_resurrected():
    out = apply_verification_outcome(
        _hyp(status=HypothesisStatus.REJECTED), _result(VerificationStatus.VERIFIED)
    )
    assert out.status == HypothesisStatus.REJECTED
    # Scores still maintained: ranking inputs stay fresh even for the terminal row.
    assert out.support_score == 0.6


def test_verified_candidate_keeps_terminal_status():
    out = apply_verification_outcome(
        _hyp(status=HypothesisStatus.VERIFIED), _result(VerificationStatus.VERIFIED)
    )
    assert out.status == HypothesisStatus.VERIFIED


def test_action_outcome_carries_fuzzy_orientation():
    hint = {"type": "fuzzy_orientation", "suggested_symbols": ["a::b"]}
    outcome = ActionOutcome(
        action_type=ActionType.SEARCH_CODE,
        fuzzy_orientation=hint,
    )
    assert outcome.fuzzy_orientation == hint
    assert ActionOutcome.model_validate(outcome.model_dump()).fuzzy_orientation == hint


def test_latest_fuzzy_orientation_newest_wins():
    old = {"miss": "ZERO_EXACT_HITS"}
    new = {"miss": "NO_VERIFIED_CANDIDATES"}
    entries = [
        {"action": "SEARCH_CODE"},
        {"action": "SEARCH_CODE", "fuzzy_orientation": old},
        {"action": "GET_CODE"},
        {"action": "SEARCH_CODE", "fuzzy_orientation": new},
    ]
    assert _latest_fuzzy_orientation(entries) == new
    assert _latest_fuzzy_orientation([{"action": "x"}]) is None
    assert _latest_fuzzy_orientation([{"fuzzy_orientation": "not-a-dict"}]) is None
    assert _latest_fuzzy_orientation(None) is None


def test_verification_status_value_handles_enum_and_string() -> None:
    from investigation_agent_platform.application.investigation.verification import (
        VerificationStatus,
    )
    from investigation_agent_platform.application.worker.activities.verification import (
        _verification_status_value,
    )

    assert (
        _verification_status_value(SimpleNamespace(status=VerificationStatus.VERIFIED))
        == "VERIFIED"
    )
    assert _verification_status_value(SimpleNamespace(status="VERIFIED")) == "VERIFIED"
    assert _verification_status_value(SimpleNamespace(status="inconclusive")) == "inconclusive"
    assert _verification_status_value(SimpleNamespace(status=None)) == ""
    assert _verification_status_value(None) == ""


@pytest.mark.asyncio
async def test_verify_malformed_investigation_id_fails_fast() -> None:
    # A malformed ID is a non-retryable client error: success=False is
    # returned directly instead of bubbling into ApplicationFailure.
    try:
        from temporalio.exceptions import ApplicationFailure
    except ImportError:
        from temporalio.exceptions import ApplicationError as ApplicationFailure

    ctx = AppContext()
    set_app_context(ctx)
    try:
        result = await verify_root_cause_activity(
            VerifyRootCauseInput(tenant_id="tenant-a", investigation_id="not-a-uuid")
        )
    except ApplicationFailure as exc:
        pytest.fail(f"malformed ID must not raise ApplicationFailure: {exc}")
    assert result.success is False
    assert "invalid investigation_id format" in (result.error or "")


@pytest.mark.asyncio
async def test_verify_no_hypotheses_emits_skipped_envelope(caplog) -> None:  # type: ignore[no-untyped-def]
    # Story integrity (live trace 3c80875c): verify started twice with zero
    # completions — the no_hypotheses early return emitted nothing, leaving
    # dangling starts. The skipped envelope must pair with the started one.
    ctx = AppContext()
    ctx.evidence_repo = SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[]))
    ctx.hypothesis_repo = SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[]))
    set_app_context(ctx)
    with caplog.at_level(
        logging.INFO,
        logger="investigation_agent_platform.application.worker.activities.verification",
    ):
        result = await verify_root_cause_activity(
            VerifyRootCauseInput(tenant_id="tenant-a", investigation_id=str(uuid4()))
        )
    assert result.success is True
    assert result.data == {"verified": False, "reason": "no_hypotheses"}
    assert "skipped; no hypotheses to verify" in caplog.text
