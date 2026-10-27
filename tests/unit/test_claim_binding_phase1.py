# tests/unit/test_claim_binding_phase1.py
"""Phase 1 (claim binding): authoritative claim-local resolution, evaporated
demotion, L3 structured subject, version pin, closed-vocabulary telemetry.

Under pin cbv1 (and unmarked) the legacy bank path decides byte-identically;
every strict-behavior test below constructs its engine with
claim_binding_version="cbv2" explicitly.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from investigation_agent_platform.application.investigation.verification import (
    CLAIM_BINDING_CBV1,
    CLAIM_BINDING_CBV2,
    CLAIM_BINDING_CURRENT,
    ClaimBindingEnforcement,
    HistorySubjectInput,
    LimitationCode,
    RootCauseVerificationPolicy,
    UnknownVerifierVersion,
    VerificationEngine,
    VerificationResult,
    VerificationStatus,
    VerifierVersionUnavailable,
    bound_kept_evidence_only,
    resolve_claim_binding_version,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.hypothesis.models import (
    AssessmentStrength,
    AssessmentType,
    ClaimKind,
    ClaimTarget,
    Hypothesis,
    HypothesisEvidenceAssessment,
    HypothesisStatus,
)
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)


def _hyp(statement="cause is X", status=HypothesisStatus.PROPOSED):
    iid = uuid4()
    return Hypothesis(
        tenant_id="tenant-a",
        investigation_id=iid,
        title=statement[:256],
        description=statement,
        statement=statement,
        status=status,
    )


def _ev(provider="prov-a", etype=EvidenceType.LOG):
    iid = uuid4()
    now = datetime.now(UTC)
    return Evidence(
        tenant_id="tenant-a",
        investigation_id=iid,
        evidence_type=etype,
        provider=provider,
        source="urn:src",
        title="t",
        summary="s",
        fingerprint=f"fp-{uuid4()}",
        observed_at=now,
        retrieved_at=now,
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


def _commit_row(path, sha, subject, observed_at, discovery_only=False, match_count=None):
    row = _ev(provider="git", etype=EvidenceType.COMMIT_HISTORY)
    attrs: dict = {}
    if discovery_only:
        attrs["discovery_only"] = True
    if match_count is not None:
        attrs["match_count"] = match_count
    return row.model_copy(
        update={
            "summary": subject,
            "observed_at": observed_at,
            "attributes": attrs,
            "provenance": row.provenance.model_copy(
                update={
                    "source_location": row.provenance.source_location.model_copy(
                        update={"file_path": path, "revision": sha}
                    )
                }
            ),
        }
    )


def _policy():
    return RootCauseVerificationPolicy()


# ---------------------------------------------------------------------------
# Pin matrix (D4)
# ---------------------------------------------------------------------------


class TestClaimBindingPin:
    def test_unmarked_resolves_to_current(self):
        version, source = resolve_claim_binding_version({})
        assert version == CLAIM_BINDING_CURRENT
        assert source == "unmarked"

    def test_pinned_values_resolve(self):
        assert resolve_claim_binding_version({"claim_binding_version": "cbv1"}) == (
            CLAIM_BINDING_CBV1,
            "pinned",
        )
        assert resolve_claim_binding_version({"claim_binding_version": "cbv2"}) == (
            CLAIM_BINDING_CBV2,
            "pinned",
        )

    def test_unknown_pin_fails_closed(self):
        with pytest.raises(UnknownVerifierVersion):
            resolve_claim_binding_version({"claim_binding_version": "v9"})

    def test_unreadable_metadata_fails_closed(self):
        with pytest.raises(VerifierVersionUnavailable):
            resolve_claim_binding_version(None)
        with pytest.raises(VerifierVersionUnavailable):
            resolve_claim_binding_version(42)

    def test_engine_defaults_to_current(self):
        assert VerificationEngine().claim_binding_version == CLAIM_BINDING_CURRENT

    def test_engine_rejects_unknown_pin(self):
        with pytest.raises(UnknownVerifierVersion):
            VerificationEngine(claim_binding_version="v9")

    def test_engine_accepts_cbv2(self):
        assert VerificationEngine(claim_binding_version="cbv2").claim_binding_version == "cbv2"


# ---------------------------------------------------------------------------
# Structured subject comparison (D2)
# ---------------------------------------------------------------------------


class TestSubjectsMatch:
    def test_exact_match(self):
        assert VerificationEngine.subjects_match("Tighten window", "Tighten window")

    def test_canonicalization_ignores_case_and_whitespace(self):
        assert VerificationEngine.subjects_match("  Tighten   WINDOW ", "tighten window")

    def test_conventional_prefix_stripped(self):
        assert VerificationEngine.subjects_match("fix(parser): handle null", "Handle NULL")

    def test_balanced_quotes_stripped(self):
        assert VerificationEngine.subjects_match("'Tighten window'", "tighten window")

    def test_quote_canonicalization_order_pinned(self):
        # Balanced outer quotes (one layer, matching kinds) strip;
        # unbalanced quotes are retained in canonical form. Token
        # comparison itself is punctuation-insensitive.
        assert VerificationEngine._canonical_subject("'Tighten window'") == "tighten window"
        assert VerificationEngine._canonical_subject("'Tighten window") == "'tighten window"

    def test_motivating_rewording_matches(self):
        assert VerificationEngine.subjects_match("with a STANDARD default", "with STANDARD default")

    def test_different_subjects_fail(self):
        assert not VerificationEngine.subjects_match(
            "Tighten eligibility window", "Guard null orderType"
        )

    def test_empty_either_side_never_matches(self):
        assert not VerificationEngine.subjects_match("", "Tighten window")
        assert not VerificationEngine.subjects_match("Tighten window", "")
        assert not VerificationEngine.subjects_match(None, None)

    def test_single_token_drift_fails(self):
        assert not VerificationEngine.subjects_match("Fix bug", "Fix crash")
        # One-word insertion in a 1-token subject: 1/2 below the floor.
        assert not VerificationEngine.subjects_match("Fix", "Fix bug")

    def test_short_subject_insertion_fails(self):
        # One-word insertion in a 2-token subject scores 2/3, below the
        # 0.70 floor — this is the generic-subject collision shape.
        assert VerificationEngine.subjects_match("Update README", "Update README twice") is False


class TestRepairCitationContract:
    def _history_target(self, path, commit, subject):
        return ClaimTarget(file_path=path, commit_id=commit, commit_subject=subject)

    def test_same_file_revision_and_subject_repairs(self):
        from investigation_agent_platform.application.investigation.verification import (
            repair_citation,
        )

        now = datetime.now(UTC)
        cited = _commit_row("service/src/Impl.java", "abc123", "Tighten window", now)
        donor = _commit_row("service/src/Impl.java", "abc123", "Tighten window", now)
        target = self._history_target("service/src/Impl.java", "abc123", "Tighten window")
        assert repair_citation(ClaimKind.HISTORY.value, target, cited, donor) is True

    def test_cross_file_never_repairs(self):
        from investigation_agent_platform.application.investigation.verification import (
            repair_citation,
        )

        now = datetime.now(UTC)
        cited = _commit_row("service/src/A.java", "abc123", "Tighten window", now)
        donor = _commit_row("service/src/B.java", "abc123", "Tighten window", now)
        target = self._history_target("service/src/A.java", "abc123", "Tighten window")
        assert repair_citation(ClaimKind.HISTORY.value, target, cited, donor) is False

    def test_cross_revision_never_repairs(self):
        from investigation_agent_platform.application.investigation.verification import (
            repair_citation,
        )

        now = datetime.now(UTC)
        cited = _commit_row("service/src/A.java", "abc123", "Tighten window", now)
        donor = _commit_row("service/src/A.java", "def456", "Tighten window", now)
        target = self._history_target("service/src/A.java", "abc123", "Tighten window")
        assert repair_citation(ClaimKind.HISTORY.value, target, cited, donor) is False

    def test_unscoped_kind_never_repairs(self):
        from investigation_agent_platform.application.investigation.verification import (
            repair_citation,
        )

        now = datetime.now(UTC)
        cited = _commit_row("service/src/A.java", "abc123", "Tighten window", now)
        donor = _commit_row("service/src/A.java", "abc123", "Tighten window", now)
        target = self._history_target("service/src/A.java", "abc123", "Tighten window")
        assert repair_citation(ClaimKind.CAUSAL.value, target, cited, donor) is False
        assert repair_citation(None, target, cited, donor) is False

    def test_no_production_caller_in_phase1(self):
        # Phase 3 (claim binding, D6): the zero-caller test is now a
        # gated-caller test. repair_citation / attempt_claim_repair may
        # only be referenced from the five enumerated repair sites and
        # their direct pure helpers — verification.py (predicate, helper,
        # engine loop) and the verify/conclude activities (fast-path
        # wraps, waiver, enforcement extension) — never from kind gates
        # or ungated paths. cbv1 behavioral zero-execution is pinned by
        # TestNoUngatedRepair in test_claim_binding_phase3.py.
        import pathlib

        allowed = {
            pathlib.Path(
                "src/investigation_agent_platform/application/investigation/verification.py"
            ),
            pathlib.Path(
                "src/investigation_agent_platform/application/worker/activities/verification.py"
            ),
            pathlib.Path(
                "src/investigation_agent_platform/application/worker/activities/lifecycle.py"
            ),
        }
        hits = []
        for path in pathlib.Path("src/investigation_agent_platform").rglob("*.py"):
            if path in allowed:
                continue
            try:
                text = path.read_text()
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if "repair_citation" in line or "attempt_claim_repair" in line:
                    hits.append(f"{path}:{i}")
        assert hits == [], f"repair helpers referenced outside gated sites: {hits}"


class TestCausalStepsCiteByConstruction:
    def test_step_citing_unknown_id_fails_closed(self):
        from investigation_agent_platform.domain.hypothesis.models import (
            HypothesisCausalStep,
        )

        ghost = uuid4()
        hyp = _hyp("Root cause chain")
        step = HypothesisCausalStep(cause="a", mechanism="b", effect="c", evidence_ids=[ghost])
        assessment = HypothesisEvidenceAssessment(
            hypothesis_id=hyp.id,
            evidence_id=ghost,
            assessment=AssessmentType.SUPPORTS,
            strength=AssessmentStrength.MODERATE,
            reason="causal link",
            claim_kind=ClaimKind.CAUSAL,
        )
        hyp = hyp.model_copy(
            update={
                "causal_steps": [step],
                "assessments": [assessment],
                "supporting_evidence_ids": [ghost],
            }
        )
        ok, reason = VerificationEngine().causal_step_grounding([step], [])
        assert ok is False
        assert "unknown" in reason
        # Under cbv2 the same shape evaporates with a CAUSAL next action.
        engine = VerificationEngine(claim_binding_version="cbv2")
        result = engine.evaluate_hypothesis(hyp, [], [], [step], _policy())
        assert result.status == VerificationStatus.INCONCLUSIVE
        assert result.diagnostics is not None
        assert [e.cited_id for e in result.diagnostics.evaporated_ids] == [str(ghost)]
        assert any(
            "causal" in (m.next_action or "").lower()
            for m in result.diagnostics.missing_obligations
        )


class TestHistorySubjectInput:
    def test_structured_input_shape(self):
        import pytest
        from pydantic import ValidationError

        inp = HistorySubjectInput(
            claimed_subject="Tighten window",
            stored_summary="Tighten window",
            target_file="service/src/Impl.java",
            target_commit="abc123",
            investigation_id=uuid4(),
        )
        assert inp.claimed_subject == "Tighten window"
        with pytest.raises(ValidationError):
            HistorySubjectInput(
                claimed_subject="x",
                stored_summary="y",
                target_file="f",
                target_commit="c",
                investigation_id="not-a-uuid",
            )


class TestSubjectMatchOutcome:
    def test_outcome_vocabulary(self):
        ve = VerificationEngine
        assert ve.subject_match_outcome("A B", "A B") == "matched_exact"
        assert ve.subject_match_outcome("with a STANDARD default", "with STANDARD default") in (
            "matched_exact",
            "matched_token_set",
        )
        assert ve.subject_match_outcome("Tighten window", "Guard nulls") == "mismatched_subject"
        assert ve.subject_match_outcome("", "Tighten window") == "missing_subject"
        assert ve.subject_match_outcome("Tighten window", "") == "missing_row"


class TestHistoryProofBranch:
    PATH = "service/src/OrderServiceImpl.java"

    def _rows(self):
        now = datetime.now(UTC)
        older = _commit_row(self.PATH, "abc11111", "Guard null orderType", now)
        latest = _commit_row(self.PATH, "def22222", "Tighten eligibility window", now)
        return older, latest, now

    def test_cbv1_legacy_predicate_unchanged(self):
        older, latest, _ = self._rows()
        target = ClaimTarget(file_path=self.PATH, commit_id="def22222")
        ok, _ = VerificationEngine.history_proof([latest, older], target, "cbv1")
        assert ok is True

    def test_cbv2_requires_structured_subject(self):
        older, latest, _ = self._rows()
        target = ClaimTarget(file_path=self.PATH, commit_id="def22222")
        ok, reason = VerificationEngine.history_proof([latest, older], target, "cbv2")
        assert ok is False
        assert "commit_subject" in reason

    def test_cbv2_structured_rewording_matches(self):
        older, latest, _ = self._rows()
        latest = latest.model_copy(update={"summary": "Tighten the eligibility window"})
        target = ClaimTarget(
            file_path=self.PATH,
            commit_id="def22222",
            commit_subject="Tighten eligibility window",
        )
        ok, _ = VerificationEngine.history_proof([latest, older], target, "cbv2")
        assert ok is True

    def test_cbv2_different_subject_fails(self):
        older, latest, _ = self._rows()
        target = ClaimTarget(
            file_path=self.PATH,
            commit_id="def22222",
            commit_subject="Guard null orderType",
        )
        ok, _ = VerificationEngine.history_proof([latest, older], target, "cbv2")
        assert ok is False

    def test_cbv2_newest_rule_preserved(self):
        older, latest, now = self._rows()
        # Newest path row carries a non-matching subject: fail closed even
        # though an older matching row exists.
        latest = latest.model_copy(update={"summary": "Unrelated refactor"})
        target = ClaimTarget(
            file_path=self.PATH,
            commit_id="def22222",
            commit_subject="Unrelated refactor",
        )
        ok, _ = VerificationEngine.history_proof([latest, older], target, "cbv2")
        # Newest row matches its own (wrong) subject claim trivially when the
        # claim names it; the gate that matters is validate_history_claim.
        assert ok is True
        # But a claim naming the OLDER commit fails: newest is not matching.
        old_target = ClaimTarget(
            file_path=self.PATH,
            commit_id="abc11111",
            commit_subject="Guard null orderType",
        )
        ok_old, _ = VerificationEngine.history_proof([latest, older], old_target, "cbv2")
        assert ok_old is False


def _history_hyp(statement, path, commit_id, commit_subject, row):
    hyp = _hyp(statement)
    target = ClaimTarget(file_path=path, commit_id=commit_id, commit_subject=commit_subject)
    assessment = HypothesisEvidenceAssessment(
        hypothesis_id=hyp.id,
        evidence_id=row.evidence_id,
        assessment=AssessmentType.SUPPORTS,
        strength=AssessmentStrength.STRONG,
        reason="history",
        claim_kind=ClaimKind.HISTORY,
        claim_target=target,
        commit_id=commit_id,
        commit_subject=commit_subject,
    )
    return hyp.model_copy(
        update={
            "assessments": [assessment],
            "supporting_evidence_ids": [row.evidence_id],
        }
    )


class TestValidateHistoryClaimBranch:
    PATH = "service/src/OrderServiceImpl.java"

    def test_cbv2_verifies_structured_rewording(self):
        now = datetime.now(UTC)
        row = _commit_row(self.PATH, "def22222", "Tighten the eligibility window", now)
        hyp = _history_hyp(
            "Something changed recently",
            self.PATH,
            "def22222",
            "Tighten eligibility window",
            row,
        )
        engine = VerificationEngine(claim_binding_version="cbv2")
        assert engine.validate_history_claim(hyp, [row], []) is not None

    def test_cbv2_rejects_mismatched_subject(self):
        now = datetime.now(UTC)
        row = _commit_row(self.PATH, "def22222", "Tighten eligibility window", now)
        hyp = _history_hyp(
            "Something changed recently",
            self.PATH,
            "def22222",
            "Guard null orderType",
            row,
        )
        engine = VerificationEngine(claim_binding_version="cbv2")
        assert engine.validate_history_claim(hyp, [row], []) is None


# ---------------------------------------------------------------------------
# Partition filter (D1)
# ---------------------------------------------------------------------------


class TestBoundKeptEvidenceOnly:
    def test_uncited_rows_pass_through_without_counting(self):
        cited = _ev()
        uncited = _ev()
        kept, evaporated, count = bound_kept_evidence_only(
            [cited, uncited], {str(cited.evidence_id)}, {str(cited.evidence_id)}
        )
        assert evaporated == []
        assert count == 1
        assert {e.evidence_id for e in kept} == {cited.evidence_id, uncited.evidence_id}

    def test_cited_but_unkept_evaporates(self):
        cited_id = str(uuid4())
        row = _ev()
        # The cited row is dropped from the working set but named in
        # evaporated (never silent). Cited IDs with no bound row at all
        # (ghost) are named by the enforcement layer, not the filter.
        kept, evaporated, count = bound_kept_evidence_only(
            [row], set(), {cited_id, str(row.evidence_id)}
        )
        assert evaporated == [str(row.evidence_id)]
        assert count == 0
        assert kept == []

    def test_statement_matched_range_row_does_not_count(self):
        # Tranche B negative control at the filter level: an uncited
        # statement-matching range row passes through kept but does not
        # count as cited survival. The verdict branch keys off
        # cited_kept_count, not raw kept emptiness.
        cited_unknown = str(uuid4())
        range_row = _ev()
        kept, evaporated, count = bound_kept_evidence_only([range_row], set(), {cited_unknown})
        assert count == 0
        assert evaporated == []
        assert kept == [range_row]


def _history_hypothesis_with_citations(path, commit_id, subject, cited_ids):
    hyp = _hyp("Latest change to Impl.java")
    target = ClaimTarget(file_path=path, commit_id=commit_id, commit_subject=subject)
    assessments = [
        HypothesisEvidenceAssessment(
            hypothesis_id=hyp.id,
            evidence_id=cid,
            assessment=AssessmentType.SUPPORTS,
            strength=AssessmentStrength.STRONG,
            reason="cited",
            claim_kind=ClaimKind.HISTORY,
            claim_target=target,
            commit_id=commit_id,
            commit_subject=subject,
        )
        for cid in cited_ids
        if isinstance(cid, UUID)
    ]
    supporting = [cid for cid in cited_ids if isinstance(cid, UUID)]
    return hyp.model_copy(
        update={"assessments": assessments, "supporting_evidence_ids": supporting}
    )


class TestClaimBindingEnforcement:
    def test_cbv1_returns_none(self):
        engine = VerificationEngine()
        hyp = _hyp()
        assert engine.claim_binding_enforcement(hyp, [_ev()], None) is None

    def test_zero_kept_fails_closed(self):
        engine = VerificationEngine(claim_binding_version="cbv2")
        ghost = uuid4()
        hyp = _history_hypothesis_with_citations(
            "service/src/Impl.java", "abc123", "Some subject", [ghost]
        )
        enf = engine.claim_binding_enforcement(hyp, [], None)
        assert enf is not None
        assert enf.cited_kept_count == 0
        assert enf.evaporated_ids == [str(ghost)]
        assert enf.next_action

    def test_bank_fallback_admitted_with_unmapped(self):
        engine = VerificationEngine(claim_binding_version="cbv2")
        row = _ev()
        hyp = _history_hypothesis_with_citations(
            "service/src/Impl.java", "abc123", "Some subject", [row.evidence_id]
        )
        enf = engine.claim_binding_enforcement(hyp, [row], None)
        assert enf is not None
        assert enf.cited_kept_count == 1
        assert enf.evaporated_ids == []
        assert enf.unmapped is True
        assert LimitationCode.UNMAPPED_CITATION.value in enf.limitation_codes

    def test_claim_local_beats_unmapped(self):
        engine = VerificationEngine(claim_binding_version="cbv2")
        row = _ev()
        hyp = _history_hypothesis_with_citations(
            "service/src/Impl.java", "abc123", "Some subject", [row.evidence_id]
        )
        recorded = [([str(row.evidence_id)], {"search_query": "Impl", "scope": None})]
        enf = engine.claim_binding_enforcement(hyp, [row], recorded)
        assert enf is not None
        assert enf.has_claim_local is True
        assert enf.unmapped is False
        assert LimitationCode.UNMAPPED_CITATION.value not in enf.limitation_codes


class TestEvaluateHypothesisEnforcement:
    def test_zero_kept_inconclusive_with_diagnostics(self):
        engine = VerificationEngine(claim_binding_version="cbv2")
        ghost = uuid4()
        hyp = _history_hypothesis_with_citations(
            "service/src/Impl.java", "abc123", "Some subject", [ghost]
        )
        result = engine.evaluate_hypothesis(hyp, [], [], [], _policy())
        assert result.status == VerificationStatus.INCONCLUSIVE
        assert result.corroborating_evidence_ids == []
        assert result.diagnostics is not None
        assert [e.cited_id for e in result.diagnostics.evaporated_ids] == [str(ghost)]
        assert len(result.diagnostics.missing_obligations) == 1
        assert "commit-history" in (result.diagnostics.missing_obligations[0].next_action or "")

    def test_zero_kept_beats_uncited_passing_bank(self):
        # Tranche B negative control end to end: an uncited banked row that
        # would satisfy a gate on its own cannot rescue zero cited-kept.
        engine = VerificationEngine(claim_binding_version="cbv2")
        ghost = uuid4()
        hyp = _history_hypothesis_with_citations(
            "service/src/Impl.java", "abc123", "Some subject", [ghost]
        )
        result = engine.evaluate_hypothesis(hyp, [_ev()], [], [], _policy())
        assert result.status == VerificationStatus.INCONCLUSIVE
        assert [e.cited_id for e in result.diagnostics.evaporated_ids] == [str(ghost)]

    def test_cbv1_legacy_shape_unchanged(self):
        engine = VerificationEngine()
        ghost = uuid4()
        hyp = _history_hypothesis_with_citations(
            "service/src/Impl.java", "abc123", "Some subject", [ghost]
        )
        result = engine.evaluate_hypothesis(hyp, [], [], [], _policy())
        assert result.status == VerificationStatus.INCONCLUSIVE
        # WP-A2 diagnostics-only behavior is preserved byte-identically:
        # evaporated named, missing obligation present, no verdict change.
        assert result.diagnostics is not None
        assert [e.cited_id for e in result.diagnostics.evaporated_ids] == [str(ghost)]


def _verified_result(hyp_id):
    from investigation_agent_platform.application.investigation.verification import (
        InvestigationConfidence,
    )

    return VerificationResult(
        hypothesis_id=hyp_id,
        status=VerificationStatus.VERIFIED,
        corroborating_evidence_ids=[],
        explanation="candidate verified",
        confidence=InvestigationConfidence(
            coverage_score=1.0,
            reliability_score=1.0,
            causal_score=0.5,
            contradiction_penalty=0.0,
        ),
    )


def _caller_hypothesis(path):
    hyp = _hyp("What calls OrderServiceHandler")
    target = ClaimTarget(symbol="OrderServiceHandler", file_path=path)
    assessment = HypothesisEvidenceAssessment(
        hypothesis_id=hyp.id,
        evidence_id=None,
        assessment=AssessmentType.SUPPORTS,
        strength=AssessmentStrength.MODERATE,
        reason="caller enumeration",
        claim_kind=ClaimKind.CALL_EDGE,
        claim_target=target,
    )
    return hyp, assessment, target


class TestEnforceClaimBindingKinds:
    def test_caller_edge_bank_fallback_only_refused(self):
        # Observed linkage exists (timeline read) but retrieved nothing
        # cited: the claim-local edge is absent by observation, not by
        # missing telemetry.
        engine = VerificationEngine(claim_binding_version="cbv2")
        row = _ev()
        other = _ev()
        hyp, assessment, _ = _caller_hypothesis("service/src/A.java")
        assessment = assessment.model_copy(update={"evidence_id": row.evidence_id})
        hyp = hyp.model_copy(
            update={
                "assessments": [assessment],
                "supporting_evidence_ids": [row.evidence_id],
            }
        )
        recorded = [([str(other.evidence_id)], {"search_query": "q", "scope": None})]
        enf = engine.claim_binding_enforcement(hyp, [row, other], recorded)
        assert enf is not None
        assert enf.unmapped is True
        assert enf.linkage_observed is True
        assert enf.has_claim_local is False
        assert LimitationCode.UNMAPPED_CITATION.value in enf.limitation_codes
        result = _verified_result(hyp.id).model_copy(
            update={"corroborating_evidence_ids": [row.evidence_id]}
        )
        out = engine.enforce_claim_binding(hyp, result, enf)
        assert out.status == VerificationStatus.INCONCLUSIVE
        assert out.corroborating_evidence_ids == []

    def test_history_bank_fallback_only_refused(self):
        engine = VerificationEngine(claim_binding_version="cbv2")
        now = datetime.now(UTC)
        row = _commit_row("service/src/Impl.java", "abc123", "Some subject", now)
        other = _ev()
        hyp = _history_hypothesis_with_citations(
            "service/src/Impl.java", "abc123", "Some subject", [row.evidence_id]
        )
        recorded = [([str(other.evidence_id)], {"search_query": "q", "scope": None})]
        enf = engine.claim_binding_enforcement(hyp, [row, other], recorded)
        assert enf is not None
        assert enf.unmapped is True
        assert enf.linkage_observed is True
        result = _verified_result(hyp.id).model_copy(
            update={"corroborating_evidence_ids": [row.evidence_id]}
        )
        out = engine.enforce_claim_binding(hyp, result, enf)
        assert out.status == VerificationStatus.INCONCLUSIVE
        assert out.corroborating_evidence_ids == []

    def test_caller_edge_claim_local_kept_passes(self):
        # The positive counterpart: observed claim-local linkage keeps the
        # VERIFIED verdict (refusal requires observed absence, not merely
        # unmapped legacy shape).
        engine = VerificationEngine(claim_binding_version="cbv2")
        row = _ev()
        hyp, assessment, _ = _caller_hypothesis("service/src/A.java")
        assessment = assessment.model_copy(update={"evidence_id": row.evidence_id})
        hyp = hyp.model_copy(
            update={
                "assessments": [assessment],
                "supporting_evidence_ids": [row.evidence_id],
            }
        )
        recorded = [([str(row.evidence_id)], {"search_query": "q", "scope": None})]
        enf = engine.claim_binding_enforcement(hyp, [row], recorded)
        assert enf is not None
        assert enf.has_claim_local is True
        assert enf.unmapped is False
        result = _verified_result(hyp.id).model_copy(
            update={"corroborating_evidence_ids": [row.evidence_id]}
        )
        out = engine.enforce_claim_binding(hyp, result, enf)
        assert out.status == VerificationStatus.VERIFIED

    def test_cross_claim_borrowing_blocked(self):
        # Claim A's fetch banked rowB; claim B cites rowB. Claim A's own
        # citation (rowA, banked but never retrieved for A) must NOT read
        # as claim-local just because the investigation retrieved rowB.
        engine = VerificationEngine(claim_binding_version="cbv2")
        row_a = _ev()
        row_b = _ev()
        hyp = _hyp("What calls Handler and where is it defined")
        asm_a = HypothesisEvidenceAssessment(
            hypothesis_id=hyp.id,
            evidence_id=row_a.evidence_id,
            assessment=AssessmentType.SUPPORTS,
            strength=AssessmentStrength.MODERATE,
            reason="caller",
            claim_kind=ClaimKind.CALL_EDGE,
            claim_target=ClaimTarget(symbol="Handler", file_path="service/src/A.java"),
        )
        asm_b = HypothesisEvidenceAssessment(
            hypothesis_id=hyp.id,
            evidence_id=row_b.evidence_id,
            assessment=AssessmentType.SUPPORTS,
            strength=AssessmentStrength.MODERATE,
            reason="definition",
            claim_kind=ClaimKind.DEFINITION,
            claim_target=ClaimTarget(symbol="Helper", file_path="service/src/B.java"),
        )
        hyp = hyp.model_copy(
            update={
                "assessments": [asm_a, asm_b],
                "supporting_evidence_ids": [row_a.evidence_id, row_b.evidence_id],
            }
        )
        # Only B's fetch is recorded: rowB's retrieval, never rowA's.
        recorded = [([str(row_b.evidence_id)], {"search_query": "Helper", "scope": None})]
        enf = engine.claim_binding_enforcement(hyp, [row_a, row_b], recorded)
        assert enf is not None
        # B's group is claim-local; A's group is bank-fallback-only.
        assert "DEFINITION" in (enf.local_kinds or [])
        assert "CALL_EDGE" in (enf.cited_kinds or [])
        assert "CALL_EDGE" not in (enf.local_kinds or [])
        # The effective (CALL_EDGE) claim therefore cannot verify on the
        # borrowed bank row.
        result = _verified_result(hyp.id).model_copy(
            update={"corroborating_evidence_ids": [row_a.evidence_id]}
        )
        out = engine.enforce_claim_binding(hyp, result, enf)
        assert out.status == VerificationStatus.INCONCLUSIVE
        assert out.corroborating_evidence_ids == []

    def test_same_kind_second_target_not_local(self):
        # MUST-ADD 1: two CALL_EDGE targets sharing the callee. Only the
        # first caller's row was retrieved. The second target must not be
        # claim-local, and the aggregate must not verify as a complete
        # enumeration. Fails on kind-level locality (CALL_EDGE marked
        # local when either target has evidence).
        engine = VerificationEngine(claim_binding_version="cbv2")
        row_a = _ev()
        row_b = _ev()
        hyp = _hyp("What calls Handler")
        mk_asm = lambda row, caller: HypothesisEvidenceAssessment(
            hypothesis_id=hyp.id,
            evidence_id=row.evidence_id,
            assessment=AssessmentType.SUPPORTS,
            strength=AssessmentStrength.MODERATE,
            reason="caller",
            claim_kind=ClaimKind.CALL_EDGE,
            claim_target=ClaimTarget(symbol="Handler", counterpart=caller),
        )
        asm_a = mk_asm(row_a, "CallerA")
        asm_b = mk_asm(row_b, "CallerB")
        hyp = hyp.model_copy(
            update={
                "assessments": [asm_a, asm_b],
                "supporting_evidence_ids": [row_a.evidence_id, row_b.evidence_id],
            }
        )
        recorded = [([str(row_a.evidence_id)], {"search_query": "Handler", "scope": None})]
        enf = engine.claim_binding_enforcement(hyp, [row_a, row_b], recorded)
        assert enf is not None
        assert enf.linkage_observed is True
        assert "CALL_EDGE" in (enf.cited_kinds or [])
        # The first target is local; the second target's group is not.
        assert "CALL_EDGE" in (enf.local_kinds or [])
        assert "CALL_EDGE" in (enf.unmapped_kinds or [])
        result = _verified_result(hyp.id).model_copy(
            update={"corroborating_evidence_ids": [row_a.evidence_id, row_b.evidence_id]}
        )
        out = engine.enforce_claim_binding(hyp, result, enf)
        assert out.status == VerificationStatus.INCONCLUSIVE
        assert out.corroborating_evidence_ids == []
        # The kept set still contains both rows for diagnostics, but the
        # verdict cannot publish on the unlinked target.
        assert set(enf.kept_ids or []) == {
            str(row_a.evidence_id),
            str(row_b.evidence_id),
        }

    def test_same_query_different_target_no_false_locality(self):
        # MUST-ADD 2: both targets derive the same search query (shared
        # callee symbol), different counterparts/scopes. The attempt
        # recorded with that exact derived query banked only the first
        # caller's row. Query matching must not make the second target
        # local: locality comes from identity overlap only.
        from investigation_agent_platform.application.evidence.retrieval_ledger import (
            derive_claim_query,
        )

        engine = VerificationEngine(claim_binding_version="cbv2")
        row_a = _ev()
        row_b = _ev()
        hyp = _hyp("What calls Handler")
        mk_asm = lambda row, caller, scope: HypothesisEvidenceAssessment(
            hypothesis_id=hyp.id,
            evidence_id=row.evidence_id,
            assessment=AssessmentType.SUPPORTS,
            strength=AssessmentStrength.MODERATE,
            reason="caller",
            claim_kind=ClaimKind.CALL_EDGE,
            claim_target=ClaimTarget(symbol="Handler", counterpart=caller, scope=scope),
        )
        asm_a = mk_asm(row_a, "CallerA", "repo:svc-a")
        asm_b = mk_asm(row_b, "CallerB", "repo:svc-b")
        hyp = hyp.model_copy(
            update={
                "assessments": [asm_a, asm_b],
                "supporting_evidence_ids": [row_a.evidence_id, row_b.evidence_id],
            }
        )
        # Both groups derive query "Handler" (shared symbol slot); prove
        # it, then record the attempt with that exact query text banking
        # only row A.
        q_a, _ = derive_claim_query(
            "What calls Handler", "CALL_EDGE", {"symbol": "Handler", "counterpart": "CallerA"}
        )
        q_b, _ = derive_claim_query(
            "What calls Handler", "CALL_EDGE", {"symbol": "Handler", "counterpart": "CallerB"}
        )
        assert q_a and q_a == q_b
        recorded = [([str(row_a.evidence_id)], {"search_query": q_a, "scope": None})]
        enf = engine.claim_binding_enforcement(hyp, [row_a, row_b], recorded)
        assert enf is not None
        local_groups = list(enf.local_groups or [])
        cited_groups = list(enf.cited_groups or [])
        assert len([g for g in cited_groups if g.startswith("CALL_EDGE|")]) == 2
        assert len([g for g in local_groups if g.startswith("CALL_EDGE|")]) == 1
        result = _verified_result(hyp.id).model_copy(
            update={"corroborating_evidence_ids": [row_a.evidence_id, row_b.evidence_id]}
        )
        out = engine.enforce_claim_binding(hyp, result, enf)
        assert out.status == VerificationStatus.INCONCLUSIVE

    def test_both_callers_linked_verifies(self):
        # MUST-ADD 3 (positive control): separate linked attempts, one per
        # target. Both edges local — the aggregate verifies, proving the
        # negative controls do not simply abstain on all multi-edge claims.
        engine = VerificationEngine(claim_binding_version="cbv2")
        row_a = _ev()
        row_b = _ev()
        hyp = _hyp("What calls Handler")
        mk_asm = lambda row, caller: HypothesisEvidenceAssessment(
            hypothesis_id=hyp.id,
            evidence_id=row.evidence_id,
            assessment=AssessmentType.SUPPORTS,
            strength=AssessmentStrength.MODERATE,
            reason="caller",
            claim_kind=ClaimKind.CALL_EDGE,
            claim_target=ClaimTarget(symbol="Handler", counterpart=caller),
        )
        hyp = hyp.model_copy(
            update={
                "assessments": [mk_asm(row_a, "CallerA"), mk_asm(row_b, "CallerB")],
                "supporting_evidence_ids": [row_a.evidence_id, row_b.evidence_id],
            }
        )
        recorded = [
            ([str(row_a.evidence_id)], {"search_query": "CallerA", "scope": None}),
            ([str(row_b.evidence_id)], {"search_query": "CallerB", "scope": None}),
        ]
        enf = engine.claim_binding_enforcement(hyp, [row_a, row_b], recorded)
        assert enf is not None
        assert "CALL_EDGE" not in (enf.unmapped_kinds or [])
        assert len([g for g in (enf.local_groups or []) if g.startswith("CALL_EDGE|")]) == 2
        result = _verified_result(hyp.id).model_copy(
            update={"corroborating_evidence_ids": [row_a.evidence_id, row_b.evidence_id]}
        )
        out = engine.enforce_claim_binding(hyp, result, enf)
        assert out.status == VerificationStatus.VERIFIED

    def test_unobserved_linkage_keeps_bank_path_with_limitation(self):
        # No timeline read (recorded=None): locality cannot be observed, so
        # the bank path decides with an UNMAPPED limitation instead of the
        # CALL_EDGE refusal firing on missing telemetry.
        engine = VerificationEngine(claim_binding_version="cbv2")
        row = _ev()
        hyp, assessment, _ = _caller_hypothesis("service/src/A.java")
        assessment = assessment.model_copy(update={"evidence_id": row.evidence_id})
        hyp = hyp.model_copy(
            update={
                "assessments": [assessment],
                "supporting_evidence_ids": [row.evidence_id],
            }
        )
        enf = engine.claim_binding_enforcement(hyp, [row], None)
        assert enf is not None
        assert enf.linkage_observed is False
        assert enf.unmapped is True
        result = _verified_result(hyp.id).model_copy(
            update={"corroborating_evidence_ids": [row.evidence_id]}
        )
        out = engine.enforce_claim_binding(hyp, result, enf)
        assert out.status == VerificationStatus.VERIFIED
        codes = [
            str(getattr(getattr(lim, "code", ""), "value", getattr(lim, "code", "")))
            for lim in out.limitations
        ]
        assert LimitationCode.UNMAPPED_CITATION.value in codes

    def test_enforcement_tail_is_load_bearing(self, monkeypatch):
        # Negative control: with the enforce tail disabled, the zero-kept
        # claim keeps the inner verdict shape without authoritative
        # evaporated/demotion records; with it, the claim is INCONCLUSIVE
        # with named evaporated IDs and a missing obligation.
        engine = VerificationEngine(claim_binding_version="cbv2")
        ghost = uuid4()
        hyp = _history_hypothesis_with_citations(
            "service/src/Impl.java", "abc123", "Some subject", [ghost]
        )
        enforced = engine.evaluate_hypothesis(hyp, [], [], [], _policy())
        assert enforced.status == VerificationStatus.INCONCLUSIVE
        assert enforced.diagnostics is not None
        assert enforced.diagnostics.demotions
        assert enforced.diagnostics.missing_obligations
        monkeypatch.setattr(
            engine,
            "enforce_claim_binding",
            lambda _h, res, _e: res,
        )
        bypassed = engine.evaluate_hypothesis(hyp, [], [], [], _policy())
        assert bypassed.diagnostics is not None
        # collect_verification_diagnostics still names evaporated IDs and
        # the missing obligation; what the tail adds is the authoritative
        # Demotion record and the evaporation explanation. Without the
        # tail, the claim abstains generically instead of by name.
        assert bypassed.diagnostics.demotions == []
        assert "evaporated" not in bypassed.explanation
        assert "evaporated" in enforced.explanation

    def test_enforcement_fault_fails_closed(self, monkeypatch):
        # A partitioning fault under cbv2 never degrades to the legacy
        # bank path: the claim is INCONCLUSIVE, never decided unpartitioned.
        engine = VerificationEngine(claim_binding_version="cbv2")
        hyp = _hyp("Some claim")
        monkeypatch.setattr(
            engine,
            "claim_binding_enforcement",
            lambda _h, _e, _r: ClaimBindingEnforcement(fault=True),
        )
        result = engine.evaluate_hypothesis(hyp, [_ev()], [], [], _policy())
        assert result.status == VerificationStatus.INCONCLUSIVE
        assert result.corroborating_evidence_ids == []
        assert "fault" in result.explanation

    def test_fault_in_enforce_fails_closed_on_verified(self):
        # The re-review residual: enforce_claim_binding must fail closed on
        # a fault enforcement even for an otherwise-VERIFIED fast-path
        # result (fast paths bypass evaluate_hypothesis's fault branch).
        engine = VerificationEngine(claim_binding_version="cbv2")
        hyp = _hyp("Some claim")
        result = _verified_result(hyp.id)
        out = engine.enforce_claim_binding(hyp, result, ClaimBindingEnforcement(fault=True))
        assert out.status == VerificationStatus.INCONCLUSIVE
        assert out.corroborating_evidence_ids == []

    def test_enforce_never_raises(self):
        engine = VerificationEngine(claim_binding_version="cbv2")
        result = _verified_result(uuid4())
        # Garbage inputs (None hypothesis, missing enforcement fields) must
        # degrade, never raise into the caller.
        out = engine.enforce_claim_binding(None, result, ClaimBindingEnforcement())
        assert out.status in (VerificationStatus.INCONCLUSIVE, VerificationStatus.VERIFIED)

    def test_none_enforcement_fails_closed_explicitly(self):
        # A None enforcement is a programming error at the choke point
        # (all call sites guarantee non-None under cbv2); it must fail
        # closed with INCONCLUSIVE and no corroborating IDs, never
        # preserve a VERIFIED input.
        engine = VerificationEngine(claim_binding_version="cbv2")
        result = _verified_result(uuid4()).model_copy(
            update={"corroborating_evidence_ids": [uuid4()]}
        )
        out = engine.enforce_claim_binding(None, result, None)  # type: ignore[arg-type]
        assert out.status == VerificationStatus.INCONCLUSIVE
        assert out.corroborating_evidence_ids == []

    def test_fault_enforcement_fails_closed_explicitly(self):
        # Tightened companion: a fault enforcement must yield INCONCLUSIVE
        # with zero corroborating IDs — never preserve a VERIFIED input.
        # An empty (uncited) enforcement preserves the existing target-
        # declared path instead.
        engine = VerificationEngine(claim_binding_version="cbv2")
        result = _verified_result(uuid4()).model_copy(
            update={"corroborating_evidence_ids": [uuid4()]}
        )
        out = engine.enforce_claim_binding(None, result, ClaimBindingEnforcement(fault=True))
        assert out.status == VerificationStatus.INCONCLUSIVE
        assert out.corroborating_evidence_ids == []
        kept = engine.enforce_claim_binding(
            None, result, ClaimBindingEnforcement(cited_ids=[], kept_ids=[])
        )
        assert kept.status == VerificationStatus.VERIFIED
        assert kept.corroborating_evidence_ids == result.corroborating_evidence_ids

    def test_empty_cited_takes_missing_obligation_path(self):
        # Empty cited sets never evaporate: no IDs, no kept, no demotion —
        # the existing missing-obligation path applies unchanged.
        engine = VerificationEngine(claim_binding_version="cbv2")
        hyp = _hyp("Latest change to Impl.java")
        enf = engine.claim_binding_enforcement(hyp, [_ev()], None)
        assert enf is not None
        assert enf.cited_ids == []
        assert enf.evaporated_ids == []
        result = engine.evaluate_hypothesis(hyp, [_ev()], [], [], _policy())
        assert result.status == VerificationStatus.INCONCLUSIVE
        assert result.diagnostics is None or not result.diagnostics.evaporated_ids

    def test_definition_bank_fallback_stays_verified_with_limitation(self):
        engine = VerificationEngine(claim_binding_version="cbv2")
        row = _ev()
        hyp = _hyp("SampleOrderFilter is defined in Impl.java")
        enf = ClaimBindingEnforcement(
            cited_ids=[str(row.evidence_id)],
            kept_ids=[str(row.evidence_id)],
            evaporated_ids=[],
            cited_kept_count=1,
            unmapped=True,
            has_claim_local=False,
            limitation_codes=[LimitationCode.UNMAPPED_CITATION.value],
            next_action=None,
        )
        result = _verified_result(hyp.id).model_copy(
            update={"corroborating_evidence_ids": [row.evidence_id]}
        )
        out = engine.enforce_claim_binding(hyp, result, enf)
        assert out.status == VerificationStatus.VERIFIED
        codes = [
            str(getattr(getattr(lim, "code", ""), "value", getattr(lim, "code", "")))
            for lim in out.limitations
        ]
        assert LimitationCode.UNMAPPED_CITATION.value in codes

    def test_corroborating_filtered_to_kept(self):
        engine = VerificationEngine(claim_binding_version="cbv2")
        kept_row = _ev()
        dropped_row = _ev()
        hyp = _hyp("Some claim")
        enf = ClaimBindingEnforcement(
            cited_ids=[str(kept_row.evidence_id)],
            kept_ids=[str(kept_row.evidence_id)],
            evaporated_ids=[],
            cited_kept_count=1,
            unmapped=False,
            has_claim_local=True,
            limitation_codes=[],
            next_action=None,
        )
        result = _verified_result(hyp.id).model_copy(
            update={"corroborating_evidence_ids": [kept_row.evidence_id, dropped_row.evidence_id]}
        )
        out = engine.enforce_claim_binding(hyp, result, enf)
        assert out.status == VerificationStatus.VERIFIED
        assert out.corroborating_evidence_ids == [kept_row.evidence_id]


# ---------------------------------------------------------------------------
# Entry-site enumeration (D6)
# ---------------------------------------------------------------------------


class TestEntrySites:
    def test_engine_fast_paths_exist_with_evidence_param(self):
        import inspect

        engine = VerificationEngine(claim_binding_version="cbv2")
        for name in (
            "validate_code_value_claim",
            "validate_java_fact_claim",
            "validate_history_claim",
            "validate_location_claim",
            "validate_doc_claim",
            "validate_state_claim",
            "evaluate_caller_edge",
            "evaluate_hypothesis",
        ):
            assert callable(getattr(engine, name, None)), name
            params = list(inspect.signature(getattr(engine, name)).parameters)
            assert "evidence_items" in params, name

    def test_activities_import_binding_symbols(self):
        from investigation_agent_platform.application.worker.activities import (
            verification as verify_activity,
        )

        assert hasattr(verify_activity, "verify_root_cause_activity")
        from investigation_agent_platform.application.worker.activities import (
            lifecycle,
        )

        assert hasattr(lifecycle, "_gate_conclusion_or_fail")

    def test_evaluate_hypothesis_accepts_recorded_attempts(self):
        import inspect

        assert (
            "recorded_attempts"
            in inspect.signature(VerificationEngine.evaluate_hypothesis).parameters
        )


# ---------------------------------------------------------------------------
# Telemetry (D5)
# ---------------------------------------------------------------------------


class TestBindingTelemetry:
    def test_limitation_allowlist_extended(self):
        from investigation_agent_platform.application.worker.activities.common import (
            _KNOWN_LIMITATION_CODES,
            _limitation_kind,
        )

        for code in (
            "NO_RUNTIME_OBSERVATION",
            "RIVAL_SHARED_EVIDENCE",
            "UNMAPPED_CITATION",
            "EVAPORATED_CITATION",
            "RIVAL_UNTESTED",
            "SINGLE_PROVIDER",
            "CONCLUSION_UNMAPPED",
            "RELATION_UNDEMONSTRATED",
        ):
            assert code in _KNOWN_LIMITATION_CODES
        assert _limitation_kind("UNMAPPED_CITATION: bank only") == "UNMAPPED_CITATION"
        assert _limitation_kind("BOGUS_CODE: detail") == "other"

    def test_port_has_binding_methods(self):
        from investigation_agent_platform.ports.observability.telemetry import (
            ObservabilityPort,
        )

        for name in (
            "record_claim_binding",
            "record_history_subject",
            "record_binding_version",
        ):
            assert hasattr(ObservabilityPort, name), name

    def test_adapter_closed_vocab_and_noop(self):
        from investigation_agent_platform.infrastructure.observability.telemetry import (
            OpenTelemetryObservabilityAdapter,
        )
        from investigation_agent_platform.ports.observability.telemetry import (
            SpanContext,
        )

        ctx = SpanContext(tenant_id="t", investigation_id=uuid4())
        disabled = OpenTelemetryObservabilityAdapter(
            service_name="test", otlp_endpoint="", enabled=False
        )
        disabled.record_claim_binding(ctx, "DEFINITION", "claim_local", "cbv2")
        disabled.record_history_subject(ctx, "matched_exact", "satisfied")
        disabled.record_binding_version(ctx, "cbv2", "pinned")
        enabled = OpenTelemetryObservabilityAdapter(
            service_name="test", otlp_endpoint="", enabled=True
        )
        # Out-of-vocabulary labels coerce; never raise.
        enabled.record_claim_binding(ctx, "DEFINITION", "claim_local", "cbv2")
        enabled.record_claim_binding(ctx, "../../../etc/passwd", "nope", "v99")
        enabled.record_history_subject(ctx, "matched_exact", "satisfied")
        enabled.record_history_subject(ctx, "evil", "evil")
        enabled.record_binding_version(ctx, "cbv2", "pinned")
        enabled.record_binding_version(ctx, "v9", "hacker")
        assert enabled._closed("DEFINITION", enabled._CLAIM_KINDS, "x") == "DEFINITION"
        assert enabled._closed("nope", enabled._CLAIM_KINDS, "fallback") == "fallback"

    def test_emit_verify_telemetry_binding_params(self):
        import inspect

        from investigation_agent_platform.application.worker.activities.common import (
            _emit_verify_telemetry,
        )

        params = inspect.signature(_emit_verify_telemetry).parameters
        for name in ("binding_version", "binding_source", "recorded", "evidence"):
            assert name in params, name

    def test_emit_verify_telemetry_binding_content(self):
        from investigation_agent_platform.application.worker.activities.common import (
            _emit_verify_telemetry,
        )

        calls: list[tuple] = []

        class _Obs:
            def record_claim_binding(self, ctx, kind, disp, ver):
                calls.append(("binding", kind, disp, ver))

            def record_history_subject(self, ctx, outcome, newest):
                calls.append(("subject", outcome, newest))

            def record_binding_version(self, ctx, version, source):
                calls.append(("version", version, source))

            def record_verification(self, ctx, *args):
                calls.append(("verification",) + tuple(args))

            def record_location_answer(self, ctx, *args):
                pass

        now = datetime.now(UTC)
        row = _commit_row("service/src/Impl.java", "abc123", "Tighten window", now)
        hyp = _history_hypothesis_with_citations(
            "service/src/Impl.java", "abc123", "Tighten window", [row.evidence_id]
        )
        recorded = [([str(row.evidence_id)], {"search_query": "q", "scope": None})]
        engine = VerificationEngine(claim_binding_version="cbv2")
        result = engine.evaluate_hypothesis(
            hyp, [row], [], [], _policy(), recorded_attempts=recorded
        )
        ctx = SimpleNamespace(observability=_Obs())
        _emit_verify_telemetry(
            ctx,
            "tenant-a",
            uuid4(),
            hyp,
            result,
            [],
            binding_version="cbv2",
            binding_source="pinned",
            recorded=recorded,
            evidence=[row],
        )
        kinds = [c for c in calls if c[0] == "binding"]
        assert kinds, "expected per-claim binding emissions"
        for _, kind, disp, ver in kinds:
            assert kind in (
                "DEFINITION",
                "CALL_EDGE",
                "BEHAVIOR",
                "ABSENCE",
                "HISTORY",
                "CAUSAL",
                "UNTYPED",
                "unspecified",
            )
            assert disp in (
                "claim_local",
                "bank_fallback",
                "evaporated",
                "unmapped",
                "no_citations",
            )
            assert ver == "cbv2"
        versions = [c for c in calls if c[0] == "version"]
        assert ("version", "cbv2", "pinned") in versions
        subjects = [c for c in calls if c[0] == "subject"]
        assert subjects, "expected HISTORY subject emission"
        for _, outcome, newest in subjects:
            assert outcome in (
                "matched_exact",
                "matched_token_set",
                "mismatched_subject",
                "missing_subject",
                "missing_row",
            )
            assert newest in ("satisfied", "violated", "not_applicable")

    def test_history_subject_missing_vocabularies(self):
        from investigation_agent_platform.application.worker.activities.common import (
            _emit_verify_telemetry,
        )

        calls: list[tuple] = []

        class _Obs:
            def record_claim_binding(self, *args):
                pass

            def record_history_subject(self, ctx, outcome, newest):
                calls.append((outcome, newest))

            def record_binding_version(self, *args):
                pass

            def record_verification(self, ctx, *args):
                pass

            def record_location_answer(self, ctx, *args):
                pass

        now = datetime.now(UTC)
        row = _commit_row("service/src/Impl.java", "abc123", "Some subject", now)
        # Empty structured subject -> missing_subject.
        hyp = _history_hypothesis_with_citations(
            "service/src/Impl.java", "abc123", "", [row.evidence_id]
        )
        engine = VerificationEngine(claim_binding_version="cbv2")
        result = engine.evaluate_hypothesis(hyp, [row], [], [], _policy())
        ctx = SimpleNamespace(observability=_Obs())
        _emit_verify_telemetry(
            ctx,
            "tenant-a",
            uuid4(),
            hyp,
            result,
            [],
            binding_version="cbv2",
            binding_source="pinned",
            recorded=None,
            evidence=[row],
        )
        assert ("missing_subject", "not_applicable") in calls
        # No corroborating rows -> missing_row.
        calls.clear()
        hyp2 = _history_hypothesis_with_citations(
            "service/src/Impl.java", "abc123", "Some subject", [row.evidence_id]
        )
        result2 = engine.evaluate_hypothesis(hyp2, [], [], [], _policy())
        _emit_verify_telemetry(
            ctx,
            "tenant-a",
            uuid4(),
            hyp2,
            result2,
            [],
            binding_version="cbv2",
            binding_source="pinned",
            recorded=None,
            evidence=[],
        )
        assert ("missing_row", "not_applicable") in calls

    def test_loop_telemetry_binding_keys(self):
        from investigation_agent_platform.application.worker.activities.inputs import (
            _derive_loop_telemetry,
        )

        telemetry = _derive_loop_telemetry(
            {
                "llm_tokens_total": 10,
                "formulated_count": 1,
                "claim_binding_kept": 3,
                "claim_binding_evaporated": 1,
                "claim_binding_unmapped": 0,
                "claim_binding_version": "cbv2",
            },
            {},
            0,
        )
        assert telemetry is not None
        assert telemetry["claim_binding_kept"] == 3
        assert telemetry["claim_binding_evaporated"] == 1
        assert telemetry["claim_binding_unmapped"] == 0
        assert telemetry["claim_binding_version"] == "cbv2"

    def test_loop_telemetry_accepts_cbv3(self):
        # Finding H: cbv3 is a legal pin (repair-applied cohort) — the
        # closed telemetry label must carry it, not drop it.
        from investigation_agent_platform.application.worker.activities.inputs import (
            _derive_loop_telemetry,
        )

        telemetry = _derive_loop_telemetry(
            {"llm_tokens_total": 1, "claim_binding_version": "cbv3"},
            {},
            0,
        )
        assert telemetry is not None
        assert telemetry["claim_binding_version"] == "cbv3"
        dropped = _derive_loop_telemetry(
            {"llm_tokens_total": 1, "claim_binding_version": "cbv9"},
            {},
            0,
        )
        assert dropped is not None
        assert "claim_binding_version" not in dropped


def _two_caller_rows():
    """Two exact CALL_EDGE range reads (Step 5-passing shape), distinct callers."""
    from investigation_agent_platform.domain.evidence.models import (
        Evidence,
        EvidenceType,
    )
    from investigation_agent_platform.domain.provenance.models import (
        EvidenceFreshness,
        EvidenceProvenance,
        QueryFingerprint,
        SourceLocation,
    )

    def _row(path, snippet):
        iid = uuid4()
        now = datetime.now(UTC)
        return Evidence(
            tenant_id="t",
            investigation_id=iid,
            evidence_type=EvidenceType.SOURCE_CODE,
            provider="test",
            source="test://src",
            title="t",
            summary="range",
            content_snippet=snippet,
            observed_at=now,
            retrieved_at=now,
            provenance=EvidenceProvenance(
                tenant_id="t",
                investigation_id=iid,
                provider_type="test",
                requested_provider_id="test",
                actual_provider_id="test",
                source_system="test",
                retrieval_timestamp=now,
                query_fingerprint=QueryFingerprint(
                    provider_type="test", operation="GET_SOURCE", normalized_query_hash="h"
                ),
                source_location=SourceLocation(
                    system="git",
                    identifier="x",
                    file_path=path,
                    line_start=10,
                    line_end=20,
                    revision="abc123",
                ),
            ),
            freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
            fingerprint=f"fp-{uuid4()}",
            attributes={"read_mode": "range"},
        )

    row_a = _row("src/OrderQualifyHelper.java", "11| return handler.handle(orderId);")
    row_b = _row("src/OrderRestProxy.java", "12| x = handler.handle(payload);")
    return row_a, row_b


def _two_caller_hyp(row_a, row_b):
    hyp = _hyp("Callers of OrderServiceHandler")
    hyp = hyp.model_copy(update={"status": HypothesisStatus.SUPPORTED})
    mk = lambda row, caller, path: HypothesisEvidenceAssessment(
        hypothesis_id=hyp.id,
        evidence_id=row.evidence_id,
        assessment=AssessmentType.SUPPORTS,
        strength=AssessmentStrength.STRONG,
        reason="caller",
        claim_kind=ClaimKind.CALL_EDGE,
        claim_target=ClaimTarget(
            symbol="OrderServiceHandler.handle",
            counterpart=caller,
            file_path=path,
            line_start=10,
            line_end=12,
            commit_id="abc123",
        ),
    )
    asm_a = mk(row_a, "OrderQualifyHelper", "src/OrderQualifyHelper.java")
    asm_b = mk(row_b, "OrderRestProxy", "src/OrderRestProxy.java")
    return hyp.model_copy(
        update={
            "assessments": [asm_a, asm_b],
            "supporting_evidence_ids": [row_a.evidence_id, row_b.evidence_id],
        }
    )


def _attempt_event(row_ids, query="OrderServiceHandler.handle"):
    return SimpleNamespace(
        event_type="RETRIEVAL_ATTEMPT",
        attributes={
            "attempt": {"search_query": query, "scope": None, "outcome": "ROWS_RETURNED"},
            "evidence_ids": [str(i) for i in row_ids],
            "attempt_id": f"att-{uuid4().hex[:8]}",
        },
    )


def _activity_ctx(inv_metadata, hyp, rows, events):
    from unittest.mock import AsyncMock

    from investigation_agent_platform.api.dependencies import AppContext, set_app_context

    ctx = AppContext()
    ctx.investigation_repo = SimpleNamespace(  # type: ignore[attr-defined]
        get_by_id=AsyncMock(
            return_value=SimpleNamespace(metadata=dict(inv_metadata), application_id="app")
        )
    )
    ctx.evidence_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=list(rows))
    )
    ctx.hypothesis_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=[hyp]),
        save=AsyncMock(),
    )
    ctx.timeline_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=list(events)),
        append=AsyncMock(return_value=None),
    )
    set_app_context(ctx)
    return ctx


class TestActivityPathsBlockUnlinkedCaller:
    @pytest.mark.asyncio
    async def test_verify_fast_path_blocks_unlinked_same_kind_caller(self):
        # MUST-ADD 4: the two-caller shape through the real verify fast
        # path. Only caller A was retrieved; caller B's banked row must
        # not satisfy its edge, and the final verdict must not be VERIFIED.
        from investigation_agent_platform.application.worker.activities import (
            VerifyRootCauseInput,
            verify_root_cause_activity,
        )

        row_a, row_b = _two_caller_rows()
        hyp = _two_caller_hyp(row_a, row_b)
        inv_id = uuid4()
        hyp = hyp.model_copy(update={"investigation_id": inv_id})
        _activity_ctx(
            {"claim_binding_version": "cbv2", "verifier_version": "v2"},
            hyp,
            [row_a, row_b],
            [_attempt_event([row_a.evidence_id])],
        )
        result = await verify_root_cause_activity(
            VerifyRootCauseInput(tenant_id="t", investigation_id=str(inv_id))
        )
        assert result.success is True
        assert result.data.get("verified") is False
        # The blocked verdict carries no caller-B proof.
        assert str(row_b.evidence_id) not in {
            str(e) for e in (result.data.get("corroborating_evidence_ids") or [])
        }

    @pytest.mark.asyncio
    async def test_conclude_denies_unmapped_caller_without_chain(self):
        # MUST-ADD 5: the same shape through the conclude gate with
        # observed retrieval history and no causal chain. Conclusion must
        # be denied (abstention), the unlinked caller must be absent from
        # proven callers, and the chain-backed allowance is preserved
        # separately (tested at engine level by
        # test_definition_bank_fallback_stays_verified_with_limitation).
        from investigation_agent_platform.application.worker.activities.lifecycle import (
            _gate_conclusion_or_fail,
        )

        row_a, row_b = _two_caller_rows()
        hyp = _two_caller_hyp(row_a, row_b)
        inv_id = uuid4()
        hyp = hyp.model_copy(update={"investigation_id": inv_id, "tenant_id": "t"})
        saved: dict = {}

        async def _save_conclusion(tenant_id, conclusion):
            saved["conclusion"] = conclusion

        ctx = _activity_ctx(
            {"claim_binding_version": "cbv2", "verifier_version": "v2"},
            hyp,
            [row_a, row_b],
            [_attempt_event([row_a.evidence_id])],
        )
        ctx.finding_repo = SimpleNamespace(  # type: ignore[attr-defined]
            save_conclusion=_save_conclusion,
        )
        status = await _gate_conclusion_or_fail(
            "t",
            inv_id,
            ctx,
            shadow_notes=None,
            triage_route="",
        )
        assert status is not None
        conclusion = saved.get("conclusion")
        assert conclusion is not None
        assert str(getattr(conclusion.status, "value", conclusion.status)) in (
            "INSUFFICIENT_EVIDENCE",
            "NO_ROOT_CAUSE_FOUND",
        )
        proven = list(getattr(conclusion, "proven_callers", None) or [])
        assert not any("OrderRestProxy" in str(p) for p in proven)
        assert any("OrderQualifyHelper" in str(p) for p in proven) or any(
            "OrderRestProxy" in str(u)
            for u in (getattr(conclusion, "unresolved_callers", None) or [])
        )

    def test_verify_choke_fault_fails_closed(self, monkeypatch):
        # NEW-1: even if enforce_claim_binding itself raised, the verify
        # choke must not leave a VERIFIED fast-path result standing.
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )

        def _boom(self, hypothesis, result, enforcement):
            raise RuntimeError("enforcement down")

        monkeypatch.setattr(VerificationEngine, "enforce_claim_binding", _boom)
        row_a, row_b = _two_caller_rows()
        hyp = _two_caller_hyp(row_a, row_b)
        inv_id = uuid4()
        hyp = hyp.model_copy(update={"investigation_id": inv_id})
        _activity_ctx(
            {"claim_binding_version": "cbv2", "verifier_version": "v2"},
            hyp,
            [row_a, row_b],
            [_attempt_event([str(row_a.evidence_id), str(row_b.evidence_id)])],
        )
        import asyncio

        from investigation_agent_platform.application.worker.activities import (
            VerifyRootCauseInput,
            verify_root_cause_activity,
        )

        result = asyncio.run(
            verify_root_cause_activity(
                VerifyRootCauseInput(tenant_id="t", investigation_id=str(inv_id))
            )
        )
        assert result.success is True
        assert result.data.get("verified") is False
        assert str(result.data.get("status", "")) == "INCONCLUSIVE"

    def test_conclude_choke_fault_fails_closed(self, monkeypatch):
        # NEW-1 conclude side: an enforcement fault forces abstention
        # instead of concluding on the unenforced fast-path verdict.
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )
        from investigation_agent_platform.application.worker.activities.lifecycle import (
            _gate_conclusion_or_fail,
        )

        def _boom(self, hypothesis, result, enforcement):
            raise RuntimeError("enforcement down")

        monkeypatch.setattr(VerificationEngine, "enforce_claim_binding", _boom)
        row_a, row_b = _two_caller_rows()
        hyp = _two_caller_hyp(row_a, row_b)
        inv_id = uuid4()
        hyp = hyp.model_copy(update={"investigation_id": inv_id, "tenant_id": "t"})
        saved: dict = {}

        async def _save(tenant_id, conclusion):
            saved["conclusion"] = conclusion

        ctx = _activity_ctx(
            {"claim_binding_version": "cbv2", "verifier_version": "v2"},
            hyp,
            [row_a, row_b],
            [_attempt_event([str(row_a.evidence_id), str(row_b.evidence_id)])],
        )
        ctx.finding_repo = SimpleNamespace(save_conclusion=_save)
        import asyncio

        status = asyncio.run(
            _gate_conclusion_or_fail("t", inv_id, ctx, shadow_notes=None, triage_route="")
        )
        assert status is not None
        conclusion = saved.get("conclusion")
        assert conclusion is not None
        assert str(getattr(conclusion, "status", "")) in (
            "INSUFFICIENT_EVIDENCE",
            "NO_ROOT_CAUSE_FOUND",
        )

    def test_conclude_publishes_unmapped_with_causal_chain(self):
        # MUST-ADD 5 (allowance half): the documented positive case — a
        # grounded causal chain permits publication with UNMAPPED_CITATION.
        # A DEFINITION claim whose only retrieval is banked-but-unmapped
        # verifies with the limitation; with a grounded causal chain the
        # gate does not need the waiver, so conclude publishes instead of
        # abstaining. This distinguishes the allowance from treating an
        # unlinked CALL_EDGE caller as proven (see the abstention test).
        # The chain is grounded: the cited range read shows the class
        # definition the step's cause/mechanism/effect describe, and the
        # row is scoped to the test investigation/tenant like a real
        # investigation-scoped lookup would return.
        from investigation_agent_platform.domain.hypothesis.models import (
            HypothesisCausalStep,
        )

        inv_id = uuid4()
        row = _ev(provider="test", etype=EvidenceType.SOURCE_CODE)
        row = row.model_copy(
            update={
                "tenant_id": "t",
                "investigation_id": inv_id,
                "content_snippet": "10| class Widget:",
                "attributes": {"read_mode": "range"},
                "provenance": row.provenance.model_copy(
                    update={
                        "tenant_id": "t",
                        "investigation_id": inv_id,
                        "query_fingerprint": row.provenance.query_fingerprint.model_copy(
                            update={"operation": "GET_SOURCE"}
                        ),
                        "source_location": row.provenance.source_location.model_copy(
                            update={
                                "file_path": "src/widgets.py",
                                "line_start": 10,
                                "line_end": 20,
                                "revision": "abc123",
                            }
                        ),
                    }
                ),
            }
        )
        causal_step = HypothesisCausalStep(
            cause="Widget class declared",
            mechanism="class definition",
            effect="Widget defined",
            evidence_ids=[row.evidence_id],
        )
        assert VerificationEngine.causal_step_grounding([causal_step], [row]) == (True, "")
        assert VerificationEngine.causal_relation_demonstrated([causal_step], [row]) == (
            True,
            "",
        )

        hyp = _hyp("Widget is defined in src/widgets.py")
        hyp = hyp.model_copy(
            update={
                "status": HypothesisStatus.SUPPORTED,
                "assessments": [
                    HypothesisEvidenceAssessment(
                        hypothesis_id=hyp.id,
                        evidence_id=row.evidence_id,
                        assessment=AssessmentType.SUPPORTS,
                        strength=AssessmentStrength.STRONG,
                        reason="definition",
                        claim_kind=ClaimKind.DEFINITION,
                        claim_target=ClaimTarget(symbol="Widget", file_path="src/widgets.py"),
                    )
                ],
                "supporting_evidence_ids": [row.evidence_id],
                "causal_steps": [causal_step],
            }
        )
        hyp = hyp.model_copy(update={"investigation_id": inv_id, "tenant_id": "t"})
        saved: dict = {}

        async def _save_conclusion(tenant_id, conclusion):
            saved["conclusion"] = conclusion

        from unittest.mock import AsyncMock

        ctx = _activity_ctx(
            {"claim_binding_version": "cbv2", "verifier_version": "v2"},
            hyp,
            [row],
            [],
        )
        ctx.finding_repo = SimpleNamespace(
            save_conclusion=_save_conclusion,
            save_finding_record=AsyncMock(),
        )
        ctx.hypothesis_repo.save = AsyncMock()
        import asyncio

        from investigation_agent_platform.application.worker.activities.lifecycle import (
            _gate_conclusion_or_fail,
        )
        from investigation_agent_platform.domain.investigation.models import (
            InvestigationStatus,
        )

        status = asyncio.run(
            _gate_conclusion_or_fail("t", inv_id, ctx, shadow_notes=None, triage_route="")
        )
        assert status == InvestigationStatus.COMPLETED
        conclusion = saved.get("conclusion")
        assert conclusion is not None
        assert "UNMAPPED_CITATION" in str(getattr(conclusion, "limitations", ""))
        assert str(getattr(conclusion, "status", "")) not in (
            "INSUFFICIENT_EVIDENCE",
            "NO_ROOT_CAUSE_FOUND",
        )

    def test_creation_paths_stamp_cbv1(self):
        import asyncio

        from investigation_agent_platform.application.knowledge.intake import (
            ErrorIntakeService,
        )

        seen: dict = {}

        class _Repo:
            async def create(self, tenant_id, investigation):
                seen["metadata"] = dict(getattr(investigation, "metadata", None) or {})

        class _Clock:
            def utcnow(self):
                return datetime.now(UTC)

        async def _run():
            svc = ErrorIntakeService(
                investigation_repo=_Repo(),
                session_repo=object(),
                code_issue_index=object(),
                clock=_Clock(),
            )
            await svc._create_investigation(
                tenant_id="t",
                application_id="a",
                problem_description="p",
                session_id="s",
                requested_by="u",
                fingerprint="f",
            )

        asyncio.run(_run())
        assert seen["metadata"].get("claim_binding_version") == "cbv1"
