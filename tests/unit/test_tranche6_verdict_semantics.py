# tests/unit/test_tranche6_verdict_semantics.py
"""Tranche 6 verdict semantics (review H2, H3, H4).

H2: sibling claims must not overwrite each other at persistence.
H3/H4: a verdict must hold for EVERY checkable (kind, target) group, and
NOT_CHECKABLE content must ship as a limitation, never as the verdict.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_claim_binding_phase2 import _formulate_ctx, _formulate_input

from investigation_agent_platform.application.worker.activities import execute_action_activity


class TestH2SiblingClaimsAreNotOverwritten:
    @staticmethod
    async def _formulate(claims, evidence_ids):  # type: ignore[no-untyped-def]
        statement = claims[0]["statement"]
        saved: list = []

        async def _save(tenant_id, hyp, investigation_id, expected_version=0):  # type: ignore[no-untyped-def]
            saved.append(hyp)

        _formulate_ctx(
            [SimpleNamespace(evidence_id=e) for e in evidence_ids],
            SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[]), save=_save),
        )
        await execute_action_activity(_formulate_input(statement, claims))
        return saved

    @pytest.mark.asyncio
    async def test_two_definition_claims_differing_only_by_symbol_both_persist(self) -> None:
        ea, eb = uuid4(), uuid4()
        claims = [
            {
                "statement": "Foo defines a and b",
                "claim_kind": "DEFINITION",
                "evidence_ids": [str(ea)],
                "target": {"symbol": "Foo.a"},
            },
            {
                "statement": "Foo defines a and b",
                "claim_kind": "DEFINITION",
                "evidence_ids": [str(eb)],
                "target": {"symbol": "Foo.b"},
            },
        ]
        saved = await self._formulate(claims, [ea, eb])
        assert len(saved) == 1
        symbols = {str(a.claim_target.symbol) for a in saved[0].assessments if a.claim_target}
        assert symbols == {"Foo.a", "Foo.b"}, f"a sibling claim was overwritten: {symbols}"
        cited = {str(a.evidence_id) for a in saved[0].assessments if a.evidence_id}
        assert cited == {str(ea), str(eb)}

    @pytest.mark.asyncio
    async def test_claims_differing_only_by_claimed_value_both_persist(self) -> None:
        ea, eb = uuid4(), uuid4()
        claims = [
            {
                "statement": "Foo defaults",
                "claim_kind": "BEHAVIOR",
                "evidence_ids": [str(ea)],
                "target": {"symbol": "Foo.retries", "claimed_value": "3"},
            },
            {
                "statement": "Foo defaults",
                "claim_kind": "BEHAVIOR",
                "evidence_ids": [str(eb)],
                "target": {"symbol": "Foo.retries", "claimed_value": "5"},
            },
        ]
        saved = await self._formulate(claims, [ea, eb])
        values = {str(a.claim_target.claimed_value) for a in saved[0].assessments if a.claim_target}
        assert values == {"3", "5"}

    @pytest.mark.asyncio
    async def test_a_true_duplicate_still_collapses_to_one(self) -> None:
        ea = uuid4()
        claim = {
            "statement": "Foo defines a and b",
            "claim_kind": "DEFINITION",
            "evidence_ids": [str(ea)],
            "target": {"symbol": "Foo.a"},
        }
        saved = await self._formulate([claim, dict(claim)], [ea])
        assert len([a for a in saved[0].assessments if a.claim_target]) == 1


# --------------------------------------------------------------------------
# H3 / H4: verdicts hold for every checkable claim group
# --------------------------------------------------------------------------

from test_claim_binding_phase3 import _def_hyp, _def_read

from investigation_agent_platform.application.investigation.verification import (
    LimitationCode,
    RootCauseVerificationPolicy,
    VerificationEngine,
    VerificationStatus,
)
from investigation_agent_platform.domain.hypothesis.models import (
    AssessmentStrength,
    AssessmentType,
    ClaimKind,
    ClaimTarget,
    HypothesisEvidenceAssessment,
)


def _asm(hyp, kind, target, evidence_id=None):  # type: ignore[no-untyped-def]
    return HypothesisEvidenceAssessment(
        hypothesis_id=hyp.id,
        evidence_id=evidence_id,
        assessment=AssessmentType.SUPPORTS,
        strength=AssessmentStrength.STRONG,
        reason="claim",
        claim_kind=kind,
        claim_target=target,
    )


def _with_assessments(hyp, extra):  # type: ignore[no-untyped-def]
    return hyp.model_copy(update={"assessments": [*hyp.assessments, *extra]})


def _verified_via_location(hyp, rows):  # type: ignore[no-untyped-def]
    engine = VerificationEngine()
    result = engine.validate_location_claim(hyp, rows, [])
    assert result is not None, "premise: the first claim verifies on the location fast path"
    assert result.status == VerificationStatus.VERIFIED
    return engine, result


class TestH3EveryCheckableGroupMustVerify:
    def test_ungrounded_causal_sibling_withdraws_a_definition_verdict(self) -> None:
        read = _def_read()
        base = _def_hyp(read)
        causal = _asm(
            base,
            ClaimKind.CAUSAL,
            ClaimTarget(symbol="pool exhaustion causes the timeout"),
            evidence_id=None,
        )
        hyp = _with_assessments(base, [causal])
        engine, verified = _verified_via_location(hyp, [read])

        out = engine.enforce_group_conjunction(
            hyp, verified, [read], [], [], RootCauseVerificationPolicy()
        )

        assert out.status == VerificationStatus.INCONCLUSIVE
        assert any(lim.code == LimitationCode.UNVERIFIED_CLAIM_GROUP for lim in out.limitations)

    def test_second_definition_citing_nothing_withdraws_the_verdict(self) -> None:
        read = _def_read()
        base = _def_hyp(read)
        second = _asm(
            base,
            ClaimKind.DEFINITION,
            ClaimTarget(symbol="otherHandler", file_path="src/alpha.py"),
            evidence_id=None,
        )
        hyp = _with_assessments(base, [second])
        engine, verified = _verified_via_location(hyp, [read])
        out = engine.enforce_group_conjunction(
            hyp, verified, [read], [], [], RootCauseVerificationPolicy()
        )
        assert out.status == VerificationStatus.INCONCLUSIVE

    def test_two_grounded_definitions_both_verify(self) -> None:
        read = _def_read()
        base = _def_hyp(read)
        # Second group cites the same range read for a symbol it contains.
        second = _asm(
            base,
            ClaimKind.DEFINITION,
            ClaimTarget(symbol="handleRequest", file_path="src/alpha.py", line_start=40),
            evidence_id=read.evidence_id,
        )
        hyp = _with_assessments(base, [second])
        engine, verified = _verified_via_location(hyp, [read])
        out = engine.enforce_group_conjunction(
            hyp, verified, [read], [], [], RootCauseVerificationPolicy()
        )
        assert out.status == VerificationStatus.VERIFIED

    def test_single_group_hypothesis_is_untouched(self) -> None:
        read = _def_read()
        hyp = _def_hyp(read)
        engine, verified = _verified_via_location(hyp, [read])
        out = engine.enforce_group_conjunction(
            hyp, verified, [read], [], [], RootCauseVerificationPolicy()
        )
        assert out is verified

    def test_non_verified_results_pass_through(self) -> None:
        read = _def_read()
        hyp = _def_hyp(read)
        engine = VerificationEngine()
        inconclusive = engine._fault_inconclusive(hyp)
        out = engine.enforce_group_conjunction(
            hyp, inconclusive, [read], [], [], RootCauseVerificationPolicy()
        )
        assert out is inconclusive


class TestH4NotCheckableNeverRidesAVerdict:
    def test_not_checkable_siblings_are_recorded_as_a_limitation(self) -> None:
        read = _def_read()
        base = _def_hyp(read)
        nc = [
            _asm(base, ClaimKind.NOT_CHECKABLE, ClaimTarget(symbol="this design is elegant")),
            _asm(base, ClaimKind.NOT_CHECKABLE, ClaimTarget(symbol="the naming is ideal")),
        ]
        hyp = _with_assessments(base, nc)
        engine, verified = _verified_via_location(hyp, [read])
        out = engine.enforce_group_conjunction(
            hyp, verified, [read], [], [], RootCauseVerificationPolicy()
        )
        assert out.status == VerificationStatus.VERIFIED, "the checkable claim still stands"
        codes = [lim.code for lim in out.limitations]
        assert LimitationCode.NOT_CHECKABLE_EXCLUDED in codes
        detail = next(
            lim.detail for lim in out.limitations if lim.code == LimitationCode.NOT_CHECKABLE_EXCLUDED
        )
        assert "2" in detail

    def test_limitation_is_not_duplicated(self) -> None:
        read = _def_read()
        base = _def_hyp(read)
        hyp = _with_assessments(
            base, [_asm(base, ClaimKind.NOT_CHECKABLE, ClaimTarget(symbol="elegant"))]
        )
        engine, verified = _verified_via_location(hyp, [read])
        once = engine.enforce_group_conjunction(
            hyp, verified, [read], [], [], RootCauseVerificationPolicy()
        )
        twice = engine.enforce_group_conjunction(
            hyp, once, [read], [], [], RootCauseVerificationPolicy()
        )
        assert [lim.code for lim in twice.limitations].count(
            LimitationCode.NOT_CHECKABLE_EXCLUDED
        ) == 1

    def test_no_not_checkable_means_no_limitation(self) -> None:
        read = _def_read()
        hyp = _def_hyp(read)
        engine, verified = _verified_via_location(hyp, [read])
        out = engine.enforce_group_conjunction(
            hyp, verified, [read], [], [], RootCauseVerificationPolicy()
        )
        assert LimitationCode.NOT_CHECKABLE_EXCLUDED not in [lim.code for lim in out.limitations]


# --------------------------------------------------------------------------
# Choke points: verify activity and conclude gate apply the conjunction
# --------------------------------------------------------------------------


def _mixed(read):  # type: ignore[no-untyped-def]
    base = _def_hyp(read)
    base = base.model_copy(update={"tenant_id": "t"})
    causal = _asm(
        base,
        ClaimKind.CAUSAL,
        ClaimTarget(symbol="pool exhaustion causes the timeout"),
        evidence_id=None,
    )
    return _with_assessments(base, [causal])


def _single(read):  # type: ignore[no-untyped-def]
    return _def_hyp(read).model_copy(update={"tenant_id": "t"})


class TestChokePointsApplyTheConjunction:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("pin", ["cbv1", "cbv3"])
    async def test_verify_activity(self, pin: str) -> None:
        import test_claim_binding_phase1 as fixtures

        from investigation_agent_platform.application.worker.activities import (
            VerifyRootCauseInput,
            verify_root_cause_activity,
        )

        outcomes = {}
        for label, build in (("single", _single), ("mixed", _mixed)):
            read = _def_read()
            hyp = build(read)
            inv_id = uuid4()
            hyp = hyp.model_copy(update={"investigation_id": inv_id})
            fixtures._activity_ctx(
                {"claim_binding_version": pin, "verifier_version": "v2"}, hyp, [read], []
            )
            result = await verify_root_cause_activity(
                VerifyRootCauseInput(tenant_id="t", investigation_id=str(inv_id))
            )
            assert result.success is True
            outcomes[label] = bool(result.data.get("verified"))
        assert outcomes["single"] is True, "premise: a single grounded definition verifies"
        assert outcomes["mixed"] is False, "an ungrounded sibling must withdraw the verdict"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("pin", ["cbv1", "cbv3"])
    async def test_conclude_gate(self, pin: str) -> None:
        import test_claim_binding_phase1 as fixtures

        from investigation_agent_platform.application.worker.activities.lifecycle import (
            _gate_conclusion_or_fail,
        )

        statuses = {}
        for label, build in (("single", _single), ("mixed", _mixed)):
            read = _def_read()
            hyp = build(read)
            inv_id = uuid4()
            hyp = hyp.model_copy(update={"investigation_id": inv_id})
            saved: dict = {}

            async def _save_conclusion(tenant_id, conclusion, _saved=saved):  # type: ignore[no-untyped-def]
                _saved["conclusion"] = conclusion

            async def _noop(*args, **kwargs):  # type: ignore[no-untyped-def]
                return None

            ctx = fixtures._activity_ctx(
                {"claim_binding_version": pin, "verifier_version": "v2"}, hyp, [read], []
            )
            ctx.finding_repo = SimpleNamespace(
                save_conclusion=_save_conclusion,
                save=_noop,
                save_finding=_noop,
                save_finding_record=_noop,
            )
            await _gate_conclusion_or_fail("t", inv_id, ctx, shadow_notes=None, triage_route="")
            conclusion = saved.get("conclusion")
            statuses[label] = str(getattr(conclusion, "status", "")) if conclusion else "none"
        assert statuses["single"] in {"ANSWERED", "ROOT_CAUSE_ESTABLISHED"}, statuses
        assert statuses["mixed"] in {"INSUFFICIENT_EVIDENCE", "NO_ROOT_CAUSE_FOUND"}, statuses
