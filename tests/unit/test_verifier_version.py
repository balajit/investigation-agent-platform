"""Version-contained verifier routing: unmarked rows run current behavior;
rows explicitly pinned to a recorded version keep that row's semantics."""

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


def test_one_active_dev_verifier_behavior() -> None:
    assert VERIFIER_VERSION_CURRENT == "v2"
    assert VERIFIER_VERSION_LEGACY == "v1"
    assert KNOWN_VERIFIER_VERSIONS == frozenset({"v1", "v2", "v3"})


def test_resolve_uses_pinned_version() -> None:
    # Version-contained routing: the recorded pin decides the row's
    # semantics (g4-closure/L2-D3-fix agreement). Unmarked rows resolve
    # to current; only explicit pins run their labeled version.
    assert resolve_verifier_version({"verifier_version": "v1"}) == ("v1", "pinned")
    assert resolve_verifier_version({"verifier_version": "v2"}) == ("v2", "pinned")
    assert resolve_verifier_version({"verifier_version": "v3"}) == ("v3", "pinned")


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
async def test_creation_does_not_pin_dev_verifier_version() -> None:
    saved: list[Any] = []
    investigation = await _create(_service(saved))
    assert "verifier_version" not in investigation.metadata
    assert "verifier_version" not in saved[0].metadata
    assert saved[0].metadata["profile_version"] == 3


def test_pinned_labels_resolve_to_their_row_version() -> None:
    # Explicit pins are honored per row; unmarked rows resolve to current
    # (see test_resolve_defaults_loaded_unmarked_to_current). The engine
    # boundary still exposes the unified evaluate path for current rows.
    from investigation_agent_platform.application.investigation.verification import (
        VerificationEngine,
    )

    assert resolve_verifier_version({"verifier_version": VERIFIER_VERSION_LEGACY}) == (
        VERIFIER_VERSION_LEGACY,
        "pinned",
    )
    assert resolve_verifier_version({"verifier_version": "v3"}) == (
        "v3",
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
        # Rejected: excluded from candidacy under the unified behavior.
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
async def test_unmarked_row_uses_unified_current_behavior(monkeypatch: Any) -> None:
    # Unmarked rows run the unified current behavior (ranked-candidate /
    # bound-subset routing). v1-pinned rows keep legacy routing instead
    # (see test_g3_retrieval_benefit legacy tests).
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
    assert call["hypothesis"].id == h2.id
    assert {e.evidence_id for e in call["evidence_items"]} == {h2.supporting_evidence_ids[0]}
    assert [h.id for h in call["siblings"]] == [h1.id]


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
    assert len(calls) == 1
    assert calls[0]["hypothesis"].id == h2.id
    assert {e.evidence_id for e in calls[0]["evidence_items"]} == {
        h2.supporting_evidence_ids[0]
    }
    assert [h.id for h in calls[0]["siblings"]] == [h1.id]


@pytest.mark.asyncio
async def test_verify_uses_unified_behavior_without_version_metadata(monkeypatch: Any) -> None:
    from investigation_agent_platform.application.worker.activities import (
        VerifyRootCauseInput,
        verify_root_cause_activity,
    )

    inv_id = uuid4()
    h1, _, rows, _ = _hyps(inv_id)
    from investigation_agent_platform.api.dependencies import get_app_context

    _harness({})
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
    assert result.success is True


@pytest.mark.asyncio
async def test_verify_persistence_conflict_is_typed_denial(monkeypatch: Any) -> None:
    """Remediation plan ARCH-HIGH-008: a failed verdict-state write must
    not return success-shaped. The evaluated verdict is preserved for
    forensics, but verified stays False so the loop can never treat an
    unpersisted VERIFIED as verified."""
    from investigation_agent_platform.application.investigation.verification import (
        InvestigationConfidence,
        VerificationEngine,
        VerificationResult,
        VerificationStatus,
    )
    from investigation_agent_platform.application.worker.activities import (
        VerifyRootCauseInput,
        verify_root_cause_activity,
    )

    inv_id = uuid4()
    h1, h2, rows, _ = _hyps(inv_id)
    ctx = _harness({"verifier_version": "v2"})
    new_id = uuid4()

    def _verified(
        self: Any,
        hypothesis: Any,
        evidence_items: Any,
        contradictions: Any,
        causal_chain: Any,
        policy: Any,
        sibling_hypotheses: Any = None,
        observer: Any = None,
    ) -> Any:
        return VerificationResult(
            hypothesis_id=hypothesis.id,
            status=VerificationStatus.VERIFIED,
            corroborating_evidence_ids=[new_id],
            explanation="verified",
            confidence=InvestigationConfidence(
                coverage_score=0.5,
                reliability_score=0.5,
                causal_score=0.5,
                contradiction_penalty=0.0,
            ),
        )

    monkeypatch.setattr(VerificationEngine, "evaluate_hypothesis", _verified)
    ctx.evidence_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=list(rows))
    )

    async def _conflict(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("rowcount 0")

    ctx.hypothesis_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=[h1, h2]),
        save=_conflict,
    )
    result = await verify_root_cause_activity(
        VerifyRootCauseInput(tenant_id="t", investigation_id=str(inv_id))
    )
    assert result.success is False
    assert result.data.get("reason") == "persistence_conflict"
    assert result.data.get("verified") is False
    assert result.data.get("persisted") is False
    assert result.data.get("status") == "VERIFIED"
    assert result.data.get("verifier_version_source") == "pinned"


@pytest.mark.asyncio
async def test_verify_materialization_failure_is_typed_denial(monkeypatch: Any) -> None:
    """Same bar for the outcome-application step: if the verdict cannot be
    materialized onto the candidate, the activity reports failure, not a
    success-shaped verification."""
    from investigation_agent_platform.application.investigation import verification as vmod
    from investigation_agent_platform.application.investigation.verification import (
        VerificationEngine,
    )
    from investigation_agent_platform.application.worker.activities import (
        VerifyRootCauseInput,
        verify_root_cause_activity,
    )

    inv_id = uuid4()
    h1, h2, rows, _ = _hyps(inv_id)
    ctx = _harness({"verifier_version": "v2"})

    def _boom(candidate: Any, result: Any) -> Any:
        raise RuntimeError("materialization down")

    monkeypatch.setattr(vmod, "apply_verification_outcome", _boom)
    monkeypatch.setattr(
        VerificationEngine, "evaluate_hypothesis", lambda *a, **k: _spy_result(h1.id)
    )
    ctx.evidence_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=list(rows))
    )
    ctx.hypothesis_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=[h1, h2]),
        save=AsyncMock(),
    )
    result = await verify_root_cause_activity(
        VerifyRootCauseInput(tenant_id="t", investigation_id=str(inv_id))
    )
    assert result.success is False
    assert result.data.get("reason") == "outcome_materialization_failed"
    assert result.data.get("verified") is False
    assert result.data.get("verifier_version_source") == "pinned"


@pytest.mark.asyncio
async def test_no_repository_uses_unified_version_source(monkeypatch: Any) -> None:
    """Repo-less dev execution uses the same unified behavior."""
    from investigation_agent_platform.api.dependencies import AppContext, set_app_context
    from investigation_agent_platform.application.investigation.verification import (
        VerificationEngine,
    )
    from investigation_agent_platform.application.worker.activities import (
        VerifyRootCauseInput,
        verify_root_cause_activity,
    )

    inv_id = uuid4()
    h1, h2, rows, _ = _hyps(inv_id)
    ctx = AppContext()
    ctx.investigation_repo = None  # type: ignore[attr-defined]
    ctx.evidence_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=list(rows))
    )
    ctx.hypothesis_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=[h1, h2]),
        save=AsyncMock(),
    )
    set_app_context(ctx)
    monkeypatch.setattr(
        VerificationEngine, "evaluate_hypothesis", lambda *a, **k: _spy_result(h1.id)
    )
    result = await verify_root_cause_activity(
        VerifyRootCauseInput(tenant_id="t", investigation_id=str(inv_id))
    )
    assert result.success is True
    assert result.data.get("verifier_version") == VERIFIER_VERSION_CURRENT
    assert result.data.get("verifier_version_source") == "no-repository"


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
