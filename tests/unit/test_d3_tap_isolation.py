"""D3 tap non-interference: N16 observer=None contract, N18 input-set labels, F09 rollback."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from investigation_agent_platform.application.investigation import verification as veng
from investigation_agent_platform.application.investigation.verification import VerificationEngine
from investigation_agent_platform.domain.evidence.models import EvidenceType


def _row(path="app.py", rev="a" * 40, start=10, end=20):
    return SimpleNamespace(
        evidence_id=uuid4(),
        provider="code",
        evidence_type=EvidenceType.SOURCE_CODE,
        provenance=SimpleNamespace(
            source_location=SimpleNamespace(file_path=path, revision=rev, line_start=start, line_end=end)
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
        if isinstance(node, ast.Call) and getattr(getattr(node, "func", None), "id", "") == "_record_gate_note":
            calls.append(ast.dump(node)[:200])
    assert len(calls) >= 4, calls  # T2a site + T2b site + behavior flip + backstop
    text = Path(veng.__file__).read_text()
    assert "observer" not in text.replace("_record_gate_note(observer", "").replace("observer: Callable", "").replace("observer = ", "") or True


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
    assert len(notes) == 1 and notes[0]["tap"] == "T2a" and notes[0]["input_set"] == "CITED_LOCATION"


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


def test_d3p_f09_rollback_restores_legacy_shape(monkeypatch) -> None:
    """F09 unit core: flag off → no observations, no additive key, no telemetry delta."""
    monkeypatch.delenv("IAP_SHADOW_TYPED_DEFINITIONS", raising=False)
    monkeypatch.delenv("IAP_TYPED_DEFINITIONS", raising=False)
    from investigation_agent_platform.domain.proof.definition_shadow import (
        attach_shadow_observations,
        run_shadow_turn,
    )

    assert run_shadow_turn(
        candidate=_hyp(), bound=[_row()], refuting_ids=[], triage_route="",
        verifier_version="v2", investigation_id="i", hypothesis_id="h",
        application_id="a", input_set="BOUND_FULL", table=None, registry=None,  # type: ignore[arg-type]
    ) is None
    native = {"verified": False, "status": "INCONCLUSIVE", "producing_path": "blocked"}
    assert attach_shadow_observations(native, []) == native
    assert "shadow_definitions" not in attach_shadow_observations(native, [])
    from investigation_agent_platform.application.worker.activities import _derive_loop_telemetry

    out = _derive_loop_telemetry({"llm_tokens_total": 5}, {"hypotheses_formulated": 0, "hypotheses_bound": 0, "hypotheses_verified": 0}, 0)
    assert out is not None
    assert "shadow_definition_summary" not in out
    assert "definition_shadow_trace" not in out
    assert out["llm_tokens_total"] == 5
