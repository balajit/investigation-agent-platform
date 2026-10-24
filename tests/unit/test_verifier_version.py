"""Verifier version containment: recorded-version routing, fail-closed.

Pinned rows keep their semantics; successfully loaded unmarked rows
resolve to current under the documented migration policy; anything
else (missing row, unreadable repo, malformed metadata, unknown pin)
fails closed instead of guessing a contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from investigation_agent_platform.application.investigation.services import (
    CreateInvestigationService,
)
from investigation_agent_platform.application.investigation.verification import (
    KNOWN_VERIFIER_VERSIONS,
    VERIFIER_VERSION_CURRENT,
    VERIFIER_VERSION_LEGACY,
    UnknownVerifierVersion,
    VerifierVersionUnavailable,
    resolve_verifier_version,
)
from investigation_agent_platform.domain.investigation.models import InvestigationRequest


def test_version_constants_are_distinct_and_current_is_v2() -> None:
    assert VERIFIER_VERSION_CURRENT == "v2"
    assert VERIFIER_VERSION_LEGACY == "v1"
    assert VERIFIER_VERSION_CURRENT != VERIFIER_VERSION_LEGACY
    assert KNOWN_VERIFIER_VERSIONS == frozenset({"v1", "v2"})


def test_resolve_uses_pinned_version() -> None:
    assert resolve_verifier_version({"verifier_version": "v1"}) == ("v1", "pinned")
    assert resolve_verifier_version({"verifier_version": "v2"}) == ("v2", "pinned")


def test_resolve_defaults_loaded_unmarked_to_current() -> None:
    assert resolve_verifier_version({}) == (VERIFIER_VERSION_CURRENT, "unmarked")
    assert resolve_verifier_version({"verifier_version": ""}) == (
        VERIFIER_VERSION_CURRENT,
        "unmarked",
    )
    assert resolve_verifier_version({"other": 1}) == (VERIFIER_VERSION_CURRENT, "unmarked")


def test_resolve_rejects_unknown_pins() -> None:
    with pytest.raises(UnknownVerifierVersion):
        resolve_verifier_version({"verifier_version": "v9"})
    with pytest.raises(UnknownVerifierVersion):
        resolve_verifier_version({"verifier_version": "legacy"})


def test_resolve_rejects_unobtainable_metadata() -> None:
    with pytest.raises(VerifierVersionUnavailable):
        resolve_verifier_version(None)
    with pytest.raises(VerifierVersionUnavailable):
        resolve_verifier_version("v1")
    with pytest.raises(VerifierVersionUnavailable):
        resolve_verifier_version(["v2"])


def _profile() -> Any:
    return SimpleNamespace(
        version=3,
        environment="production",
        investigation=SimpleNamespace(default_time_window=3600),
        status=SimpleNamespace(value="ACTIVE"),
    )


def _service(saved: list[Any]) -> CreateInvestigationService:
    profile = _profile()
    profile_repo = SimpleNamespace(
        get_by_application_id=AsyncMock(return_value=profile),
        list_revisions=AsyncMock(return_value=[profile]),
    )
    investigation_repo = SimpleNamespace(create=AsyncMock(side_effect=lambda t, i: saved.append(i)))
    return CreateInvestigationService(
        investigation_repo=investigation_repo,  # type: ignore[arg-type]
        profile_repo=profile_repo,  # type: ignore[arg-type]
    )


async def _create(service: CreateInvestigationService) -> Any:
    request = InvestigationRequest(
        problem_description="latency spike after deploy",
        application_id="example-app",
        session_id="sess-v",
        requested_by="tester",
    )
    return await service.execute(request, "tenant-a", skip_disposition=True)


@pytest.mark.asyncio
async def test_creation_pins_current_verifier_version() -> None:
    saved: list[Any] = []
    investigation = await _create(_service(saved))
    assert investigation.metadata["verifier_version"] == VERIFIER_VERSION_CURRENT
    assert saved[0].metadata["verifier_version"] == VERIFIER_VERSION_CURRENT
    assert saved[0].metadata["profile_version"] == 3


def test_version_routing_prefers_pinned_over_default() -> None:
    from investigation_agent_platform.application.investigation.verification import (
        VerificationEngine,
    )

    assert resolve_verifier_version({"verifier_version": VERIFIER_VERSION_LEGACY}) == (
        VERIFIER_VERSION_LEGACY,
        "pinned",
    )
    engine = VerificationEngine()
    assert hasattr(engine, "evaluate_hypothesis")
    assert hasattr(engine, "_evaluate_hypothesis_inner")


# --- T3: routing regression evidence at the engine boundary ---

def _harness(inv_metadata: Any, fail_get: bool = False, row_missing: bool = False):
    from investigation_agent_platform.api.dependencies import AppContext, set_app_context

    ctx = AppContext()

    async def _get(tenant_id: str, investigation_id: Any) -> Any:
        if fail_get:
            raise RuntimeError("repo down")
        if row_missing:
            return None
        return SimpleNamespace(metadata=inv_metadata)

    ctx.investigation_repo = SimpleNamespace(get_by_id=_get)  # type: ignore[attr-defined]
    ctx.evidence_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=[]),
    )
    ctx.hypothesis_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=[]),
        save=AsyncMock(),
    )
    set_app_context(ctx)
    return ctx


def _hyps(inv_id: Any) -> tuple[Any, Any, list[Any], list[Any]]:
    from investigation_agent_platform.domain.hypothesis.models import (
        Hypothesis,
        HypothesisCausalStep,
        HypothesisStatus,
    )

    e1, e2 = uuid4(), uuid4()
    h1 = Hypothesis(
        tenant_id="t",
        investigation_id=inv_id,
        title="weak",
        statement="weak claim",
        description="d",
        # Rejected: excluded from candidacy, so the ranked candidate is
        # deterministically h2 while v1 still evaluates hypotheses[0].
        status=HypothesisStatus.REJECTED,
        confidence_score=0.1,
        support_score=0.1,
    )
    h2 = Hypothesis(
        tenant_id="t",
        investigation_id=inv_id,
        title="strong",
        statement="strong claim",
        description="d",
        status=HypothesisStatus.SUPPORTED,
        confidence_score=0.9,
        support_score=0.9,
        supporting_evidence_ids=[e1],
        causal_steps=[
            HypothesisCausalStep(
                cause="load",
                mechanism="cache miss",
                effect="outage",
                evidence_ids=[e1],
            )
        ],
    )
    rows = [SimpleNamespace(evidence_id=e1), SimpleNamespace(evidence_id=e2)]
    return h1, h2, rows, [e1, e2]


def _spy_result(hyp_id: Any) -> Any:
    from investigation_agent_platform.application.investigation.verification import (
        InvestigationConfidence,
        VerificationResult,
        VerificationStatus,
    )

    return VerificationResult(
        hypothesis_id=hyp_id,
        status=VerificationStatus.INCONCLUSIVE,
        corroborating_evidence_ids=[],
        explanation="spy",
        confidence=InvestigationConfidence(
            coverage_score=0.0,
            reliability_score=0.0,
            causal_score=0.0,
            contradiction_penalty=0.0,
        ),
    )


@pytest.mark.asyncio
async def test_v1_pin_evaluates_first_hypothesis_whole_corpus(monkeypatch: Any) -> None:
    from investigation_agent_platform.application.investigation.verification import (
        VerificationEngine,
    )
    from investigation_agent_platform.application.worker.activities import (
        VerifyRootCauseInput,
        verify_root_cause_activity,
    )

    inv_id = uuid4()
    h1, h2, rows, _ = _hyps(inv_id)
    _harness({"verifier_version": "v1"})
    from investigation_agent_platform.api.dependencies import get_app_context

    ctx = get_app_context()
    ctx.evidence_repo = SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=list(rows))
    )
    ctx.hypothesis_repo = SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=[h1, h2]),
        save=AsyncMock(),
    )
    calls: list[dict[str, Any]] = []

    def _spy(self: Any, hypothesis: Any, evidence_items: Any, contradictions: Any,
             causal_chain: Any, policy: Any, sibling_hypotheses: Any = None,
             observer: Any = None) -> Any:
        calls.append({
            "hypothesis": hypothesis,
            "evidence_items": list(evidence_items or []),
            "contradictions": list(contradictions or []),
            "causal_chain": list(causal_chain or []),
            "siblings": list(sibling_hypotheses or []),
        })
        return _spy_result(hypothesis.id)

    monkeypatch.setattr(VerificationEngine, "evaluate_hypothesis", _spy)
    result = await verify_root_cause_activity(
        VerifyRootCauseInput(tenant_id="t", investigation_id=str(inv_id))
    )
    assert result.success is True
    assert len(calls) == 1
    call = calls[0]
    assert call["hypothesis"].id == h1.id
    assert {e.evidence_id for e in call["evidence_items"]} == {
        e.evidence_id for e in rows
    }
    assert call["contradictions"] == []
    assert call["causal_chain"] == []
    assert [h.id for h in call["siblings"]] == [h2.id]


@pytest.mark.asyncio
async def test_unmarked_row_evaluates_ranked_candidate_bound_subset(monkeypatch: Any) -> None:
    from investigation_agent_platform.application.investigation.verification import (
        VerificationEngine,
    )
    from investigation_agent_platform.application.worker.activities import (
        VerifyRootCauseInput,
        verify_root_cause_activity,
    )

    inv_id = uuid4()
    h1, h2, rows, _ = _hyps(inv_id)
    _harness({})
    from investigation_agent_platform.api.dependencies import get_app_context

    ctx = get_app_context()
    ctx.evidence_repo = SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=list(rows))
    )
    ctx.hypothesis_repo = SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=[h1, h2]),
        save=AsyncMock(),
    )
    calls: list[dict[str, Any]] = []

    def _spy(self: Any, hypothesis: Any, evidence_items: Any, contradictions: Any,
             causal_chain: Any, policy: Any, sibling_hypotheses: Any = None,
             observer: Any = None) -> Any:
        calls.append({"hypothesis": hypothesis, "evidence_items": list(evidence_items or []),
                      "siblings": list(sibling_hypotheses or [])})
        return _spy_result(hypothesis.id)

    monkeypatch.setattr(VerificationEngine, "evaluate_hypothesis", _spy)
    result = await verify_root_cause_activity(
        VerifyRootCauseInput(tenant_id="t", investigation_id=str(inv_id))
    )
    assert result.success is True
    # Two engine calls: the routed general evaluation, then the shadow
    # legacy comparison (which always runs hypotheses[0] + whole corpus).
    assert len(calls) == 2
    assert calls[0]["hypothesis"].id == h2.id
    assert {e.evidence_id for e in calls[0]["evidence_items"]} == {
        h2.supporting_evidence_ids[0]
    }
    assert [h.id for h in calls[0]["siblings"]] == [h1.id]
    assert calls[1]["hypothesis"].id == h1.id
    assert {e.evidence_id for e in calls[1]["evidence_items"]} == {
        e.evidence_id for e in rows
    }


@pytest.mark.asyncio
async def test_verify_fails_closed_without_version(monkeypatch: Any) -> None:
    from investigation_agent_platform.application.investigation.verification import (
        VerificationEngine,
    )
    from investigation_agent_platform.application.worker.activities import (
        VerifyRootCauseInput,
        verify_root_cause_activity,
    )

    async def _boom(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - guard
        raise AssertionError("engine must not run without a version")

    monkeypatch.setattr(VerificationEngine, "evaluate_hypothesis", _boom)
    inv_id = uuid4()
    h1, _, rows, _ = _hyps(inv_id)
    from investigation_agent_platform.api.dependencies import get_app_context

    for metadata, missing, failing in (
        ({"verifier_version": "v9"}, False, False),
        ("not-a-dict", False, False),
        ({}, True, False),
        ({}, False, True),
    ):
        _harness(metadata, fail_get=failing, row_missing=missing)
        ctx = get_app_context()
        ctx.evidence_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=list(rows))
        )
        ctx.hypothesis_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[h1]),
            save=AsyncMock(),
        )
        result = await verify_root_cause_activity(
            VerifyRootCauseInput(tenant_id="t", investigation_id=str(inv_id))
        )
        assert result.success is False, (metadata, missing, failing)
        assert "verifier version unavailable" in (result.error or "")


@pytest.mark.asyncio
async def test_conclude_denies_failed_without_version() -> None:
    from investigation_agent_platform.domain.investigation.models import InvestigationStatus

    async def _raise(tenant_id: str, investigation_id: Any) -> Any:
        raise RuntimeError("repo down")

    from investigation_agent_platform.api.dependencies import AppContext, set_app_context
    from investigation_agent_platform.application.worker.activities import (
        _gate_conclusion_or_fail,
    )

    ctx = AppContext()
    ctx.investigation_repo = SimpleNamespace(get_by_id=_raise)  # type: ignore[attr-defined]
    set_app_context(ctx)
    inv_id = uuid4()
    out = await _gate_conclusion_or_fail("t", inv_id, ctx)
    assert out == InvestigationStatus.FAILED
