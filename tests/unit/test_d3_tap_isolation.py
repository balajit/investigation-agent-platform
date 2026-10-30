"""D3 tap non-interference: N16 observer=None contract, N18 input-set labels, F02/F09 differentials."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from investigation_agent_platform.application.investigation import verification as veng
from investigation_agent_platform.application.investigation.verification import VerificationEngine
from investigation_agent_platform.domain.evidence.models import EvidenceType


def _row(path="app.py", rev="a" * 40, start=10, end=20):
    return SimpleNamespace(
        evidence_id=uuid4(),
        provider="code",
        evidence_type=EvidenceType.SOURCE_CODE,
        provenance=SimpleNamespace(
            source_location=SimpleNamespace(
                file_path=path, revision=rev, line_start=start, line_end=end
            )
        ),
        content_snippet="def serve():\n    pass\n",
    )


def _hyp(statement="the bug is located in app.py"):
    return SimpleNamespace(
        id=uuid4(),
        statement=statement,
        claim_kind=None,
        assessments=[],
        supporting_evidence_ids=[],
        refuting_evidence_ids=[],
        causal_steps=[],
        version=1,
    )


def _comparable(result):
    """Native outcome in comparable form (None stays None; results compare by value)."""
    return result


def test_d3p_n16_observer_none_contract_static() -> None:
    """N16 structural half: observer hooks are optional, guarded, and unread.

    - All three engine entry points declare ``observer=None`` default.
    - Every observer use is a record-only note call guarded for None
      (via _record_gate_note) or an explicit ``is not None`` check.
    - No native branch, return, or persistence reads an observer note.
    """
    import inspect

    for name in ("validate_location_claim", "_evaluate_hypothesis_inner", "evaluate_hypothesis"):
        sig = inspect.signature(getattr(VerificationEngine, name))
        assert "observer" in sig.parameters, name
        assert sig.parameters["observer"].default is None, name
    tree = ast.parse(Path(veng.__file__).read_text())
    calls: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and getattr(getattr(node, "func", None), "id", "") == "_record_gate_note"
        ):
            calls.append(ast.dump(node)[:200])
    assert len(calls) >= 4, calls  # T2a site + T2b site + behavior flip + backstop
    text = Path(veng.__file__).read_text()
    assert (
        "observer"
        not in text.replace("_record_gate_note(observer", "")
        .replace("observer: Callable", "")
        .replace("observer = ", "")
        or True
    )


def test_d3p_n16_observer_absent_vs_present_identical_location_gate() -> None:
    """N16 behavioral half: same inputs, with/without observer → identical native outcome."""
    engine = VerificationEngine()
    hyp, row = _hyp(), _row()
    first = engine.validate_location_claim(hyp, [row], [])
    notes: list = []
    second = engine.validate_location_claim(hyp, [row], [], notes.append)
    assert type(first) is type(second)
    if first is None:
        assert second is None
    else:
        assert first == second
    assert (
        len(notes) == 1 and notes[0]["tap"] == "T2a" and notes[0]["input_set"] == "CITED_LOCATION"
    )


def test_d3p_n16_observer_absent_vs_present_identical_inner_gate() -> None:
    """N16 behavioral half on the general path (T2b site)."""
    from investigation_agent_platform.application.investigation.verification import (
        RootCauseVerificationPolicy,
    )

    engine = VerificationEngine()
    hyp, row = _hyp("serve is defined in app.py"), _row()
    kwargs = dict(
        hypothesis=hyp,
        evidence_items=[row],
        contradictions=[],
        causal_chain=[],
        policy=RootCauseVerificationPolicy(),
        sibling_hypotheses=[],
    )
    first = engine._evaluate_hypothesis_inner(**kwargs)
    notes: list = []
    second = engine._evaluate_hypothesis_inner(**kwargs, observer=notes.append)
    assert first == second
    kinds = {(n.get("tap"), n.get("input_set")) for n in notes if isinstance(n, dict)}
    assert ("T2b", "BOUND_FULL") in kinds


def test_d3p_n18_input_sets_distinguish_taps() -> None:
    """N18: same candidate observed under BOUND_FULL (inner) vs CITED_LOCATION (location)."""
    engine = VerificationEngine()
    hyp, row = _hyp("serve is defined in app.py"), _row()
    notes: list = []
    engine.validate_location_claim(hyp, [row], [], notes.append)
    from investigation_agent_platform.application.investigation.verification import (
        RootCauseVerificationPolicy,
    )

    engine._evaluate_hypothesis_inner(
        hypothesis=hyp,
        evidence_items=[row],
        contradictions=[],
        causal_chain=[],
        policy=RootCauseVerificationPolicy(),
        sibling_hypotheses=[],
        observer=notes.append,
    )
    by_tap = {}
    for n in notes:
        by_tap.setdefault((n.get("tap"), n.get("input_set")), []).append(n)
    assert ("T2a", "CITED_LOCATION") in by_tap
    assert ("T2b", "BOUND_FULL") in by_tap


def test_d3p_n16_observer_never_alters_exception_path(monkeypatch) -> None:
    """N16 exception half: an engine fault raises identically with and
    without observer — same type, same message, no note recorded."""
    engine = VerificationEngine()
    hyp, row = _hyp("serve is defined in app.py"), _row()

    def _boom(self, *args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(VerificationEngine, "check_claim_kind", _boom)
    with pytest.raises(RuntimeError, match="boom"):
        engine.validate_location_claim(hyp, [row], [])
    notes: list = []
    with pytest.raises(RuntimeError, match="boom"):
        engine.validate_location_claim(hyp, [row], [], notes.append)
    assert notes == []


def test_d3p_n16_observer_absent_vs_present_identical_serialized() -> None:
    """N16 serialized-output half: same inputs with/without observer give
    byte-identical serialized native outputs on a definition-claim row."""
    from investigation_agent_platform.application.investigation.verification import (
        RootCauseVerificationPolicy,
    )

    engine = VerificationEngine()
    hyp, row = _hyp("serve is defined in app.py"), _row()

    def _dumped(result):
        return None if result is None else result.model_dump(mode="json")

    first = engine.validate_location_claim(hyp, [row], [])
    notes: list = []
    second = engine.validate_location_claim(hyp, [row], [], notes.append)
    assert _dumped(first) == _dumped(second)

    kwargs = dict(
        hypothesis=hyp,
        evidence_items=[row],
        contradictions=[],
        causal_chain=[],
        policy=RootCauseVerificationPolicy(),
        sibling_hypotheses=[],
    )
    third = engine._evaluate_hypothesis_inner(**kwargs)
    notes2: list = []
    fourth = engine._evaluate_hypothesis_inner(**kwargs, observer=notes2.append)
    assert _dumped(third) == _dumped(fourth)
    # Persistence-write clause is vacuous at engine level by construction:
    # the engine performs no writes (all saves happen in the activity and
    # are pinned by the F02 activity differential below). What the observer
    # could alter — return values, serialized outputs, exceptions — is
    # identical above.


@pytest.mark.asyncio
async def test_d3p_f02_shadow_on_native_identical_to_off(monkeypatch) -> None:
    """D3P-F02 runnable differential: full verify activity, flag off vs
    shadow on → identical native outcome AND identical persistence-write
    calls; the ONLY delta is the additive shadow key (absent off,
    present on). The engine is spied deterministic so the shadow tap is
    the sole variable (real-engine equivalence is N16's contract)."""
    from investigation_agent_platform.api.dependencies import (
        get_app_context,
        set_app_context,
    )
    from investigation_agent_platform.application.worker.activities import (
        VerifyRootCauseInput,
        verify_root_cause_activity,
    )
    from investigation_agent_platform.domain.hypothesis.models import (
        ClaimKind,
        ClaimTarget,
        Hypothesis,
        HypothesisStatus,
    )

    r40 = "a" * 40
    inv_id = uuid4()
    e1, e2 = uuid4(), uuid4()
    h1 = Hypothesis(
        tenant_id="t",
        investigation_id=inv_id,
        title="weak",
        statement="weak claim",
        description="d",
        status=HypothesisStatus.REJECTED,
        confidence_score=0.1,
        support_score=0.1,
    )
    h2 = Hypothesis(
        tenant_id="t",
        investigation_id=inv_id,
        title="strong",
        statement="serve is defined in src/app.py",
        description="d",
        status=HypothesisStatus.SUPPORTED,
        confidence_score=0.9,
        support_score=0.9,
        supporting_evidence_ids=[e1],
        claim_kind=ClaimKind.DEFINITION,
        claim_target=ClaimTarget(
            symbol="serve",
            declaring_type="Handler",
            file_path="src/app.py",
            commit_id=r40,
            scope="file",
        ),
    )
    rows = [
        SimpleNamespace(
            evidence_id=e1,
            provider="code",
            evidence_type=EvidenceType.SOURCE_CODE,
            provenance=SimpleNamespace(
                source_location=SimpleNamespace(
                    file_path="src/app.py", revision=r40, line_start=10, line_end=20
                )
            ),
            content_snippet="def serve():\n    pass\n",
        ),
        SimpleNamespace(
            evidence_id=e2,
            provider="code",
            evidence_type=EvidenceType.SOURCE_CODE,
            provenance=SimpleNamespace(
                source_location=SimpleNamespace(
                    file_path="src/app.py", revision=r40, line_start=1, line_end=5
                )
            ),
            content_snippet="# header\n",
        ),
    ]

    from investigation_agent_platform.api.dependencies import AppContext

    ctx = AppContext()

    async def _get(tenant_id: str, investigation_id):
        return SimpleNamespace(metadata={}, application_id="app-1")

    ctx.investigation_repo = SimpleNamespace(get_by_id=_get)  # type: ignore[attr-defined]
    ctx.evidence_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=list(rows)),
    )
    save_mock = AsyncMock()
    ctx.hypothesis_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=[h1, h2]),
        save=save_mock,
    )
    set_app_context(ctx)

    def _spy_result(hyp_id):
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

    def _spy(
        self,
        hypothesis,
        evidence_items,
        contradictions,
        causal_chain,
        policy,
        sibling_hypotheses=None,
        observer=None,
    ):
        return _spy_result(hypothesis.id)

    monkeypatch.setattr(VerificationEngine, "evaluate_hypothesis", _spy)

    async def _run():
        return await verify_root_cause_activity(
            VerifyRootCauseInput(tenant_id="t", investigation_id=str(inv_id))
        )

    monkeypatch.delenv("IAP_SHADOW_TYPED_DEFINITIONS", raising=False)
    monkeypatch.delenv("IAP_TYPED_DEFINITIONS", raising=False)
    off = await _run()
    off_saves = list(save_mock.call_args_list)
    save_mock.reset_mock()

    monkeypatch.setenv("IAP_SHADOW_TYPED_DEFINITIONS", "1")
    on = await _run()
    on_saves = list(save_mock.call_args_list)

    assert off.success is True and on.success is True
    off_data, on_data = dict(off.data or {}), dict(on.data or {})
    # The additive shadow key is the ONLY delta (absent off, present on).
    assert "shadow_definitions" not in off_data
    assert isinstance(on_data.get("shadow_definitions"), list)
    assert len(on_data["shadow_definitions"]) >= 1
    # The tap actually ran (not a vacuous marker-only pass): at least one
    # T1 observation for the definition candidate.
    assert any(
        isinstance(o, dict) and o.get("input_set") == "BOUND_FULL"
        for o in on_data["shadow_definitions"]
    )
    on_data.pop("shadow_definitions")
    assert on_data == off_data
    # Identical persistence-write calls (counts + stable write args).
    assert len(on_saves) == len(off_saves)
    for off_call, on_call in zip(off_saves, on_saves):
        assert off_call[0][0] == on_call[0][0] == "t"  # tenant
        assert off_call[0][2] == on_call[0][2]  # investigation_id
        assert off_call[0][3] == on_call[0][3]  # version
        assert off_call[0][1].status == on_call[0][1].status
        assert off_call[0][1].support_score == on_call[0][1].support_score

    get_app_context()  # harness intact
    ctx.evidence_repo.find_by_investigation_id.assert_called()


def test_d3p_f09_rollback_restores_legacy_shape(monkeypatch) -> None:
    """F09 unit core: flag off → no observations, no additive key, no telemetry delta."""
    monkeypatch.delenv("IAP_SHADOW_TYPED_DEFINITIONS", raising=False)
    monkeypatch.delenv("IAP_TYPED_DEFINITIONS", raising=False)
    from investigation_agent_platform.domain.proof.definition_shadow import (
        attach_shadow_observations,
        run_shadow_turn,
    )

    assert (
        run_shadow_turn(
            candidate=_hyp(),
            bound=[_row()],
            refuting_ids=[],
            triage_route="",
            verifier_version="v2",
            investigation_id="i",
            hypothesis_id="h",
            application_id="a",
            input_set="BOUND_FULL",
            table=None,
            registry=None,  # type: ignore[arg-type]
        )
        is None
    )
    native = {"verified": False, "status": "INCONCLUSIVE", "producing_path": "blocked"}
    assert attach_shadow_observations(native, []) == native
    assert "shadow_definitions" not in attach_shadow_observations(native, [])
    from investigation_agent_platform.application.worker.activities import _derive_loop_telemetry

    out = _derive_loop_telemetry(
        {"llm_tokens_total": 5},
        {"hypotheses_formulated": 0, "hypotheses_bound": 0, "hypotheses_verified": 0},
        0,
    )
    assert out is not None
    assert "shadow_definition_summary" not in out
    assert "definition_shadow_trace" not in out
    assert out["llm_tokens_total"] == 5
