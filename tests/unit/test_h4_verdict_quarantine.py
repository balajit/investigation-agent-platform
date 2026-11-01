# tests/unit/test_h4_verdict_quarantine.py
"""H4 quarantine: NOT_CHECKABLE content never reaches verdict fields (H4-D1).

Only when NOT_CHECKABLE claims were excluded is the published statement
rebuilt from the checkable groups; with none excluded the original ships
byte for byte. Gated: no LV_H stamp is flipped by this tranche; the live
H4-T14 gate still runs under G-T6.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_claim_binding_phase1 import _activity_ctx, _attempt_event
from test_claim_binding_phase3 import _def_hyp, _def_read

OPINION = "is the best-designed handler in the service"
STATEMENT = f"handleRequest is defined in alpha.py and {OPINION}"


def _mixed_hyp(inv_id):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.hypothesis.models import (
        AssessmentStrength,
        AssessmentType,
        ClaimKind,
        ClaimTarget,
        HypothesisEvidenceAssessment,
    )

    read = _def_read()
    hyp = _def_hyp(read, statement=STATEMENT)
    hyp = hyp.model_copy(update={"investigation_id": inv_id, "tenant_id": "t"})
    nc = HypothesisEvidenceAssessment(
        hypothesis_id=hyp.id,
        evidence_id=None,
        assessment=AssessmentType.NEUTRAL,
        strength=AssessmentStrength.WEAK,
        reason="taste judgment",
        claim_kind=ClaimKind.NOT_CHECKABLE,
        claim_target=ClaimTarget(),
    )
    return hyp.model_copy(update={"assessments": [*hyp.assessments, nc]}), read


def _gate(metadata, hyp, rows, events, finding_repo=None, engine=None):  # type: ignore[no-untyped-def]

    inv_id = hyp.investigation_id
    saved: dict = {}

    async def _save_conclusion(tenant_id, conclusion):  # type: ignore[no-untyped-def]
        saved["conclusion"] = conclusion

    async def _save_finding_record(tenant_id, finding):  # type: ignore[no-untyped-def]
        saved.setdefault("findings", []).append(finding)

    async def _noop(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    ctx = _activity_ctx(dict(metadata), hyp, list(rows), list(events))
    ctx.finding_repo = finding_repo or SimpleNamespace(
        save_conclusion=_save_conclusion,
        save=_noop,
        save_finding=_noop,
        save_finding_record=_save_finding_record,
    )
    if engine is not None:
        ctx.engine = engine  # type: ignore[attr-defined]
    return inv_id, ctx, saved


def _lv_h_metadata():  # type: ignore[no-untyped-def]
    return {"claim_binding_version": "cbv3", "verifier_version": "v2", "loop_version": "lv10"}


def _lv7_metadata():  # type: ignore[no-untyped-def]
    return {"claim_binding_version": "cbv3", "verifier_version": "v2", "loop_version": "lv7"}


async def _run_gate(metadata, engine=None):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.worker.activities.lifecycle import (
        _gate_conclusion_or_fail,
    )

    inv_id = uuid4()
    hyp, read = _mixed_hyp(inv_id)
    inv_id, ctx, saved = _gate(
        metadata, hyp, [read], [_attempt_event([read.evidence_id])], engine=engine
    )
    status = await _gate_conclusion_or_fail("t", inv_id, ctx)
    return status, saved


# --- pure builder -----------------------------------------------------------


def test_h4t04_no_nc_claim_is_byte_identical() -> None:
    from test_claim_binding_phase3 import _def_hyp as _dh
    from test_claim_binding_phase3 import _def_read as _dr

    from investigation_agent_platform.application.investigation.verdict_statement import (
        build_verdict_statement,
    )

    read = _dr()
    hyp = _dh(read, statement="handleRequest is defined in alpha.py")
    vs = build_verdict_statement(hyp)
    assert vs.text == "handleRequest is defined in alpha.py"
    assert vs.redacted is False


def test_h4t05_causal_plus_nc_renders_from_the_chain() -> None:
    from investigation_agent_platform.application.investigation.verdict_statement import (
        build_verdict_statement,
    )
    from investigation_agent_platform.application.investigation.verification import (
        CausalRelationship,
    )
    from investigation_agent_platform.domain.hypothesis.models import ClaimKind

    hyp = SimpleNamespace(
        statement="everything is slow because of the pool",
        assessments=[
            SimpleNamespace(claim_kind=ClaimKind.CAUSAL),
            SimpleNamespace(claim_kind=ClaimKind.NOT_CHECKABLE, claim_target=None),
        ],
    )
    chain = [
        CausalRelationship(
            cause="pool exhaustion", mechanism="timeouts", effect="slow responses",
            confidence=0.9,
        ),
        CausalRelationship(
            cause="slow responses", mechanism="retries", effect="pileup", confidence=0.8
        ),
    ]
    vs = build_verdict_statement(hyp, chain)
    assert vs.basis == ("CAUSAL",)
    assert "Pool exhaustion leads to slow responses" in vs.text
    assert "best-designed" not in vs.text


def test_h4t06_history_and_absence_templates() -> None:
    from investigation_agent_platform.application.investigation.verdict_statement import (
        build_verdict_statement,
    )
    from investigation_agent_platform.domain.hypothesis.models import ClaimKind, ClaimTarget

    hyp = SimpleNamespace(
        statement="history plus absence plus an opinion nobody can check",
        assessments=[
            SimpleNamespace(
                claim_kind=ClaimKind.HISTORY,
                claim_target=ClaimTarget(
                    file_path="src/Order.java",
                    commit_id="abcdef1234567890",
                    commit_subject="fix it",
                ),
            ),
            SimpleNamespace(
                claim_kind=ClaimKind.ABSENCE,
                claim_target=ClaimTarget(symbol="legacyHook", scope="src/legacy"),
            ),
            SimpleNamespace(claim_kind=ClaimKind.NOT_CHECKABLE, claim_target=None),
        ],
    )
    vs = build_verdict_statement(hyp)
    assert "abcdef123456" in vs.text and "abcdef1234567890" not in vs.text
    assert "legacyHook" in vs.text and "src/legacy" in vs.text
    assert vs.redacted is True


def test_h4t07_call_edge_aggregates_per_caller() -> None:
    from investigation_agent_platform.application.investigation.verdict_statement import (
        build_verdict_statement,
    )
    from investigation_agent_platform.domain.hypothesis.models import ClaimKind, ClaimTarget

    def _edge(caller):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            claim_kind=ClaimKind.CALL_EDGE,
            claim_target=ClaimTarget(counterpart=caller, symbol="handle"),
        )

    hyp = SimpleNamespace(
        statement="callers plus an uncheckable aside",
        assessments=[_edge("A"), _edge("B"),
                     SimpleNamespace(claim_kind=ClaimKind.NOT_CHECKABLE, claim_target=None)],
    )
    vs = build_verdict_statement(hyp)
    assert vs.text == "A calls handle. B calls handle."


def test_h4t08_nothing_templatable_falls_back_to_neutral() -> None:
    from investigation_agent_platform.application.investigation.verdict_statement import (
        NEUTRAL_STATEMENT,
        build_verdict_statement,
    )
    from investigation_agent_platform.domain.hypothesis.models import ClaimKind

    hyp = SimpleNamespace(
        statement="it feels wrong",
        assessments=[
            SimpleNamespace(
                claim_kind=ClaimKind.BEHAVIOR,
                claim_target=SimpleNamespace(symbol=None, claimed_value=None),
            ),
            SimpleNamespace(claim_kind=ClaimKind.NOT_CHECKABLE, claim_target=None),
        ],
    )
    vs = build_verdict_statement(hyp)
    assert vs.text == NEUTRAL_STATEMENT
    assert vs.fell_back is True
    assert "feels wrong" not in vs.text


def test_h4t09_builder_fault_fails_closed() -> None:
    from investigation_agent_platform.application.investigation.verdict_statement import (
        NEUTRAL_STATEMENT,
        build_verdict_statement,
    )

    # The builder's own except: a fault reading the statement must never
    # ship the raw text. (An assessments-property fault is absorbed earlier
    # inside typed_claim_groups, which guards it to []; that layer's
    # fail-open-on-unreadable is deliberate and unchanged.)
    class _Boom:
        @property
        def statement(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("store down")

        assessments = []

    vs = build_verdict_statement(_Boom())
    assert vs.text == NEUTRAL_STATEMENT
    assert vs.fell_back is True


def test_h4t10_hostile_target_text_is_bounded_and_single_line() -> None:
    from investigation_agent_platform.application.investigation.verdict_statement import (
        build_verdict_statement,
    )
    from investigation_agent_platform.domain.hypothesis.models import ClaimKind, ClaimTarget

    hostile = "sym\nbol\n" + "x" * 5000
    hyp = SimpleNamespace(
        statement="hostile plus uncheckable",
        assessments=[
            SimpleNamespace(
                # model_construct: bypass the 256-char field cap the same way
                # hostile rows reach the builder (persistence never validated
                # them); the builder must still bound and flatten.
                claim_kind=ClaimKind.DEFINITION,
                claim_target=ClaimTarget.model_construct(symbol=hostile, file_path="a.py"),
            ),
            SimpleNamespace(claim_kind=ClaimKind.NOT_CHECKABLE, claim_target=None),
        ],
    )
    vs = build_verdict_statement(hyp)
    assert "\n" not in vs.text
    assert len(vs.text) <= 2048


# --- gate integration -------------------------------------------------------


@pytest.mark.asyncio
async def test_h4t01_opinion_absent_from_root_cause() -> None:
    status, saved = await _run_gate(_lv_h_metadata())
    assert str(status) == "COMPLETED"
    conclusion = saved.get("conclusion")
    assert conclusion is not None
    assert "ANSWERED" in str(conclusion.status)
    assert "HandleRequest is defined in src/alpha.py" in (conclusion.root_cause or "")
    assert OPINION not in (conclusion.root_cause or "")
    assert any("NOT_CHECKABLE_EXCLUDED" in str(lim) for lim in (conclusion.limitations or []))


@pytest.mark.asyncio
async def test_h4t02_finding_statement_and_title_quarantined() -> None:
    _, saved = await _run_gate(_lv_h_metadata())
    conclusion = saved.get("conclusion")
    findings = saved.get("findings", [])
    assert conclusion is not None and findings
    finding = findings[0]
    assert finding.statement == conclusion.root_cause
    assert finding.title == (conclusion.root_cause or "")[:256]
    assert OPINION not in (finding.title or "")


@pytest.mark.asyncio
async def test_h4t03_older_pin_keeps_the_old_statement() -> None:
    status, saved = await _run_gate(_lv7_metadata())
    assert str(status) == "COMPLETED"
    conclusion = saved.get("conclusion")
    assert conclusion is not None
    assert conclusion.root_cause == STATEMENT


@pytest.mark.asyncio
async def test_h4t11_mapping_runs_over_the_published_text() -> None:
    _, saved = await _run_gate(_lv_h_metadata())
    conclusion = saved.get("conclusion")
    assert conclusion is not None
    limitations = [str(lim) for lim in (conclusion.limitations or [])]
    assert not any(
        "CONCLUSION_UNMAPPED" in lim and "best-designed" in lim for lim in limitations
    ), limitations


@pytest.mark.asyncio
async def test_h4t12_correlation_edge_reason_is_quarantined(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import investigation_agent_platform.infrastructure.correlation.repository as _repo_mod

    recorded: dict = {}

    class _FakeRelRepo:
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            pass

        async def record_edges(self, tenant_id, inv_id, app_id, edges):  # type: ignore[no-untyped-def]
            recorded["edges"] = list(edges)
            return len(edges)

    monkeypatch.setattr(_repo_mod, "PostgresRelationshipRepository", _FakeRelRepo)
    _, saved = await _run_gate(_lv_h_metadata(), engine=object())
    edges = recorded.get("edges", [])
    assert edges, "the gate records CORROBORATES edges for a verified verdict"
    reasons = [(e.metadata or {}).get("reason", "") for e in edges]
    assert reasons and all(r == (saved["conclusion"].root_cause or "")[:500] for r in reasons)
    assert all(OPINION not in r for r in reasons)


def test_h4t13_grader_xfails_unchanged() -> None:
    # The five strict xfails were rewritten as passing assertions when the
    # T9 grader (r2) learned to score them; this pins that none of them
    # regressed to XPASS-strict or FAIL.
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/unit/test_h3h4_battery_fixtures.py", "-q",
         "-k", "l14 or l15 or l16"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[2]),
    )
    assert proc.returncode == 0, proc.stdout[-2000:]
    assert "xfailed" not in proc.stdout
    assert "XPASS" not in proc.stdout and "failed" not in proc.stdout.lower()
