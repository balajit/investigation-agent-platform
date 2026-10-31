# tests/unit/test_claim_binding_phase3.py
"""Phase 3 (claim binding): misattribution repair behind its sub-gate.

Covers the D1/D2 pure helpers, the per-group repair loop, the five
wired sites (engine + fast-path wraps + conclude waiver), the cbv3 pin
matrix with shadow parity, closed-vocabulary telemetry, marker
serialization with the fail-closed truncation gate, and the updated
no-ungated-repair enumeration.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investigation_agent_platform.application.investigation.verification import (
    CLAIM_BINDING_CBV3,
    CLAIM_BINDING_CURRENT,
    KNOWN_CLAIM_BINDING_VERSIONS,
    ClaimBindingEnforcement,
    LimitationCode,
    RepairedBinding,
    RootCauseVerificationPolicy,
    UnknownVerifierVersion,
    VerificationEngine,
    VerificationResult,
    VerificationStatus,
    VerifierVersionUnavailable,
    _content_matches_for_repair,
    _paths_match_for_repair,
    attempt_claim_repair,
    build_verify_evaluation_attributes,
    repair_citation,
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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _code_row(op, path, rev, snippet, start=None, end=None, read_mode=None):
    iid = uuid4()
    now = datetime.now(UTC)
    attrs: dict = {}
    if read_mode is not None:
        attrs["read_mode"] = read_mode
    return Evidence(
        tenant_id="t",
        investigation_id=iid,
        evidence_type=EvidenceType.SOURCE_CODE,
        provider="test",
        source="test://src",
        title="t",
        summary="s",
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
                provider_type="test", operation=op, normalized_query_hash="h"
            ),
            source_location=SourceLocation(
                system="git",
                identifier="x",
                file_path=path,
                line_start=start,
                line_end=end,
                revision=rev,
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=f"fp-{uuid4()}",
        attributes=attrs,
    )


def _def_hit(path="src/alpha.py", rev="abc123"):
    # Windowless SEARCH_CODE hit row: cited, banked, fails the gate.
    return _code_row(
        "SEARCH_CODE",
        path,
        rev,
        "40| def handleRequest(req):",
    )


def _def_read(path="src/alpha.py", rev="abc123"):
    # GET_CODE range read whose window contains the hit snippet.
    return _code_row(
        "GET_CODE",
        path,
        rev,
        "40| def handleRequest(req):\n41|     validate(req)\n42|     return dispatch(req)",
        start=40,
        end=42,
        read_mode="range",
    )


def _def_target(path="src/alpha.py"):
    return ClaimTarget(symbol="handleRequest", file_path=path)


def _def_hyp(hit_row, statement="The request handler is defined", target=None):
    iid = uuid4()
    hyp = Hypothesis(
        tenant_id="t",
        investigation_id=iid,
        title=statement[:256],
        description=statement,
        statement=statement,
        status=HypothesisStatus.PROPOSED,
    )
    asm = HypothesisEvidenceAssessment(
        hypothesis_id=hyp.id,
        evidence_id=hit_row.evidence_id,
        assessment=AssessmentType.SUPPORTS,
        strength=AssessmentStrength.STRONG,
        reason="defines handler",
        claim_kind=ClaimKind.DEFINITION,
        claim_target=target or _def_target(),
    )
    return hyp.model_copy(
        update={
            "assessments": [asm],
            "supporting_evidence_ids": [hit_row.evidence_id],
        }
    )


def _policy():
    return RootCauseVerificationPolicy()


def _loc_row(content_snippet, start=None, end=None):
    provenance = SimpleNamespace(
        source_location=SimpleNamespace(
            line_start=start,
            line_end=end,
        )
        if start is not None
        else None
    )
    return SimpleNamespace(content_snippet=content_snippet, provenance=provenance)


# ---------------------------------------------------------------------------
# D2: content rule
# ---------------------------------------------------------------------------


class TestContentRule:
    def test_containment_without_ranges(self):
        donor = _loc_row("def handleRequest(req): validate(req) dispatch(req)")
        candidate = _loc_row("def handleRequest(req):")
        assert _content_matches_for_repair(candidate, donor) == "snippet_containment"

    def test_windowless_hit_repairs_via_containment(self):
        donor = _loc_row("40| def handleRequest(req):\n41|     validate(req)", start=40, end=42)
        candidate = _loc_row("40| def handleRequest(req):")
        assert _content_matches_for_repair(candidate, donor) == "snippet_containment"

    def test_overlap_plus_containment(self):
        donor = _loc_row("xx def f(): yy", start=10, end=20)
        candidate = _loc_row("def f()", start=15, end=25)
        assert _content_matches_for_repair(candidate, donor) == "overlap_and_containment"

    def test_overlap_without_containment_refuses(self):
        donor = _loc_row("completely different body here", start=10, end=20)
        candidate = _loc_row("def other():", start=15, end=25)
        assert _content_matches_for_repair(candidate, donor) is None

    def test_disjoint_refuses(self):
        donor = _loc_row("aaa bbb", start=1, end=5)
        candidate = _loc_row("zzz yyy", start=50, end=60)
        assert _content_matches_for_repair(candidate, donor) is None

    def test_empty_candidate_snippet_refuses(self):
        donor = _loc_row("some content", start=1, end=5)
        candidate = _loc_row("", start=1, end=5)
        assert _content_matches_for_repair(candidate, donor) is None

    def test_case_sensitive(self):
        donor = _loc_row("DEF HANDLEREQUEST(REQ):")
        candidate = _loc_row("def handleRequest(req):")
        assert _content_matches_for_repair(candidate, donor) is None

    def test_never_raises(self):
        assert _content_matches_for_repair(None, None) is None
        assert _content_matches_for_repair(object(), object()) is None
        assert _content_matches_for_repair(_loc_row("x"), None) is None


# ---------------------------------------------------------------------------
# D1: attempt_claim_repair
# ---------------------------------------------------------------------------


class TestAttemptClaimRepair:
    def _ids(self, hit, donor):
        from investigation_agent_platform.application.evidence.retrieval_ledger import (
            claim_key,
        )

        ck = claim_key("The request handler is defined", "DEFINITION")
        return ck, {str(hit.evidence_id): hit, str(donor.evidence_id): donor}

    def test_hit_repaired_to_range_read_shadow(self):
        hit, donor = _def_hit(), _def_read()
        ck, bank = self._ids(hit, donor)
        rebound, repairs = attempt_claim_repair(
            "DEFINITION",
            ck,
            _def_target(),
            [str(hit.evidence_id)],
            {str(hit.evidence_id)},
            bank,
            "cbv2",
            "no range read with recorded start/end lines",
        )
        assert rebound == [str(donor.evidence_id)]
        assert len(repairs) == 1
        rep = repairs[0]
        assert rep.cited_id == str(hit.evidence_id)
        assert rep.donor_id == str(donor.evidence_id)
        assert rep.outcome == "shadow"
        assert rep.binding_version == "cbv2"
        assert rep.basis == "snippet_containment"
        from investigation_agent_platform.application.evidence.retrieval_ledger import (
            claim_ref as _ref,
        )
        from investigation_agent_platform.application.evidence.retrieval_ledger import (
            target_group_key as _tgk,
        )

        assert rep.claim_ref == _ref(ck)
        tkey, tdict = _tgk(_def_target())
        assert rep.target_ref == tkey
        assert rep.target.get("symbol") == "handleRequest"
        assert rep.target.get("file_path") == "src/alpha.py"
        assert rep.gate_reason == "no range read with recorded start/end lines"

    def test_repair_carries_donor_snippet_for_review(self):
        # Finding G: the review input needs row content, never a bare ID.
        hit, donor = _def_hit(), _def_read()
        ck, bank = self._ids(hit, donor)
        _, repairs = attempt_claim_repair(
            "DEFINITION",
            ck,
            _def_target(),
            [str(hit.evidence_id)],
            {str(hit.evidence_id)},
            bank,
            "cbv2",
            "no range read with recorded start/end lines",
        )
        assert len(repairs) == 1
        assert repairs[0].donor_snippet == (
            "40| def handleRequest(req):\n41|     validate(req)\n42|     return dispatch(req)"
        )

    def test_applied_outcome_under_cbv3(self):
        hit, donor = _def_hit(), _def_read()
        ck, bank = self._ids(hit, donor)
        rebound, repairs = attempt_claim_repair(
            "DEFINITION",
            ck,
            _def_target(),
            [str(hit.evidence_id)],
            {str(hit.evidence_id)},
            bank,
            "cbv3",
            "gate failed",
        )
        assert len(rebound) == 1
        assert repairs[0].outcome == "applied"
        assert repairs[0].binding_version == "cbv3"

    def test_cited_donor_pool_excluded(self):
        # A row cited by another group is never offered as a donor.
        hit, donor = _def_hit(), _def_read()
        ck, bank = self._ids(hit, donor)
        rebound, repairs = attempt_claim_repair(
            "DEFINITION",
            ck,
            _def_target(),
            [str(hit.evidence_id)],
            {str(hit.evidence_id), str(donor.evidence_id)},
            bank,
            "cbv2",
            "r",
        )
        assert rebound == [] and repairs == []

    def test_ghost_candidate_never_repairs(self):
        hit, donor = _def_hit(), _def_read()
        ck, bank = self._ids(hit, donor)
        rebound, repairs = attempt_claim_repair(
            "DEFINITION",
            ck,
            _def_target(),
            ["ghost-id"],
            {"ghost-id"},
            bank,
            "cbv2",
            "r",
        )
        assert rebound == [] and repairs == []

    def test_cap_three(self):
        donor = _def_read()
        hits = [_def_hit() for _ in range(4)]
        # Distinct snippet per hit is unnecessary: each hit is contained.
        from investigation_agent_platform.application.evidence.retrieval_ledger import (
            claim_key,
        )

        ck = claim_key("The request handler is defined", "DEFINITION")
        bank = {str(donor.evidence_id): donor}
        cids = []
        for h in hits:
            bank[str(h.evidence_id)] = h
            cids.append(str(h.evidence_id))
        rebound, repairs = attempt_claim_repair(
            "DEFINITION",
            ck,
            _def_target(),
            cids,
            set(cids),
            bank,
            "cbv2",
            "r",
            max_repairs=3,
        )
        # Cap counts performed rebinds (3 records); one shared donor
        # appears once in the rebound ID list.
        assert len(repairs) == 3
        assert rebound == [str(donor.evidence_id)]

    def test_kind_scope(self):
        hit, donor = _def_hit(), _def_read()
        ck, bank = self._ids(hit, donor)
        for kind in ("CALL_EDGE", "CAUSAL", None):
            rebound, repairs = attempt_claim_repair(
                kind,
                ck,
                _def_target(),
                [str(hit.evidence_id)],
                {str(hit.evidence_id)},
                bank,
                "cbv2",
                "r",
            )
            assert rebound == [] and repairs == []

    def test_cross_file_and_revision_refuse(self):
        hit = _def_hit(path="src/a.py")
        donor = _def_read(path="src/b.py")
        ck, bank = self._ids(hit, donor)
        rebound, repairs = attempt_claim_repair(
            "DEFINITION",
            ck,
            _def_target("src/a.py"),
            [str(hit.evidence_id)],
            {str(hit.evidence_id)},
            bank,
            "cbv2",
            "r",
        )
        assert rebound == [] and repairs == []
        donor2 = _def_read(path="src/a.py", rev="def456")
        bank2 = {str(hit.evidence_id): hit, str(donor2.evidence_id): donor2}
        rebound, repairs = attempt_claim_repair(
            "DEFINITION",
            ck,
            _def_target("src/a.py"),
            [str(hit.evidence_id)],
            {str(hit.evidence_id)},
            bank2,
            "cbv2",
            "r",
        )
        assert rebound == [] and repairs == []

    def test_donor_scan_order_sorted_ids(self):
        from investigation_agent_platform.application.evidence.retrieval_ledger import (
            claim_key,
        )

        ck = claim_key("The request handler is defined", "DEFINITION")
        hit = _def_hit()
        donors = [_def_read() for _ in range(3)]
        bank = {str(hit.evidence_id): hit}
        for d in donors:
            bank[str(d.evidence_id)] = d
        cids = [str(hit.evidence_id)]
        rebound, _ = attempt_claim_repair(
            "DEFINITION",
            ck,
            _def_target(),
            cids,
            set(cids),
            bank,
            "cbv2",
            "r",
        )
        assert rebound == [sorted(str(d.evidence_id) for d in donors)[:1][0]]

    def test_literal_rejection(self):
        with pytest.raises(ValidationError):
            RepairedBinding(
                claim_ref="abc",
                cited_id="c",
                donor_id="d",
                basis="bogus_basis",
                outcome="shadow",
                binding_version="cbv2",
            )
        with pytest.raises(ValidationError):
            RepairedBinding(
                claim_ref="abc",
                cited_id="c",
                donor_id="d",
                basis="snippet_containment",
                outcome="maybe",
                binding_version="cbv2",
            )
        with pytest.raises(ValidationError):
            RepairedBinding(
                claim_ref="abc",
                cited_id="c",
                donor_id="d",
                basis="snippet_containment",
                outcome="shadow",
                binding_version="cbv9",
            )


# ---------------------------------------------------------------------------
# Per-group loop: repair_failed_groups
# ---------------------------------------------------------------------------


class TestRepairFailedGroups:
    def test_cbv1_never_runs(self):
        hit, donor = _def_hit(), _def_read()
        hyp = _def_hyp(hit)
        engine = VerificationEngine(claim_binding_version="cbv1")
        rebound, repairs = engine.repair_failed_groups(hyp, [hit, donor])
        assert rebound == [] and repairs == []
        assert engine._repair_calls == 0

    def test_cbv2_shadow_records_donor(self):
        hit, donor = _def_hit(), _def_read()
        hyp = _def_hyp(hit)
        engine = VerificationEngine(claim_binding_version="cbv2")
        rebound, repairs = engine.repair_failed_groups(hyp, [hit, donor])
        assert rebound == [str(donor.evidence_id)]
        assert len(repairs) == 1
        assert repairs[0].outcome == "shadow"
        assert engine._repair_calls == 1

    def test_cited_but_kept_wrong_row_repairs(self):
        # The motivating case: the hit is KEPT (bank-fallback) yet fails
        # the gate. Kept status must not exclude it from candidacy.
        hit, donor = _def_hit(), _def_read()
        hyp = _def_hyp(hit)
        engine = VerificationEngine(claim_binding_version="cbv2")
        enf = engine.claim_binding_enforcement(hyp, [hit, donor], None)
        assert enf is not None
        assert str(hit.evidence_id) in set(enf.kept_ids or [])
        rebound, repairs = engine.repair_failed_groups(hyp, [hit, donor])
        assert rebound == [str(donor.evidence_id)]
        assert len(repairs) == 1

    def test_vetoes(self):
        hit, donor = _def_hit(), _def_read()
        hyp = _def_hyp(hit)
        engine = VerificationEngine(claim_binding_version="cbv3")
        # Concession.
        hyp_c = hyp.model_copy(update={"statement": "cause unknown, could not determine"})
        assert engine.repair_failed_groups(hyp_c, [hit, donor]) == ([], [])
        # Blocking contradiction.
        contra = SimpleNamespace(
            resolution_status="UNRESOLVED",
            severity="HIGH",
        )
        assert engine.repair_failed_groups(hyp, [hit, donor], [contra]) == ([], [])
        resolved = SimpleNamespace(resolution_status="RESOLVED", severity="HIGH")
        rebound, _ = engine.repair_failed_groups(hyp, [hit, donor], [resolved])
        assert rebound == [str(donor.evidence_id)]
        # Refuting evidence.
        hyp_r = hyp.model_copy(update={"refuting_evidence_ids": [uuid4()]})
        assert engine.repair_failed_groups(hyp_r, [hit, donor]) == ([], [])
        # Verified sibling (rival block).
        sib = SimpleNamespace(status="VERIFIED")
        assert engine.repair_failed_groups(hyp, [hit, donor], None, [sib]) == ([], [])
        sib2 = SimpleNamespace(status="SUPPORTED")
        rebound, _ = engine.repair_failed_groups(hyp, [hit, donor], None, [sib2])
        assert rebound == [str(donor.evidence_id)]

    def test_gate_passing_group_no_attempt(self):
        # Citing the range read directly: gate passes, counter stays zero.
        donor = _def_read()
        hyp = _def_hyp(donor)
        engine = VerificationEngine(claim_binding_version="cbv3")
        rebound, repairs = engine.repair_failed_groups(hyp, [donor])
        assert rebound == [] and repairs == []
        assert engine._repair_calls == 0

    def test_per_group_authorization_isolation(self):
        # Group A (symbol Alpha) fails; donor grounds B (symbol Beta) but
        # not A. A must not repair; B repairs with its own record.
        hit_a = _code_row("SEARCH_CODE", "src/alpha.py", "abc123", "10| def Alpha():")
        donor_b = _code_row(
            "GET_CODE",
            "src/alpha.py",
            "abc123",
            "10| def Alpha():\n11| def Beta():\n12|     pass",
            start=10,
            end=12,
            read_mode="range",
        )
        iid = uuid4()
        hyp = Hypothesis(
            tenant_id="t",
            investigation_id=iid,
            title="t",
            description="d",
            statement="Two definitions",
            status=HypothesisStatus.PROPOSED,
        )
        mk = lambda row, sym: HypothesisEvidenceAssessment(
            hypothesis_id=hyp.id,
            evidence_id=row.evidence_id,
            assessment=AssessmentType.SUPPORTS,
            strength=AssessmentStrength.STRONG,
            reason="r",
            claim_kind=ClaimKind.DEFINITION,
            claim_target=ClaimTarget(symbol=sym, file_path="src/alpha.py"),
        )
        hyp = hyp.model_copy(
            update={
                "assessments": [mk(hit_a, "GammaMissing"), mk(donor_b, "Beta")],
                "supporting_evidence_ids": [hit_a.evidence_id, donor_b.evidence_id],
            }
        )
        engine = VerificationEngine(claim_binding_version="cbv2")
        rebound, repairs = engine.repair_failed_groups(hyp, [hit_a, donor_b])
        # Group GammaMissing: donor fails its gate (no Gamma symbol) and
        # is cited by group B -> excluded from the pool entirely.
        assert rebound == [] and repairs == []

    def test_reuse_across_groups_independently_gated(self):
        # One uncited donor grounds two groups: each repairs with its own
        # record and gate run (deterministic group order).
        donor = _code_row(
            "GET_CODE",
            "src/alpha.py",
            "abc123",
            "10| def Alpha():\n11| def Beta():\n12|     pass",
            start=10,
            end=12,
            read_mode="range",
        )
        hit_a = _code_row("SEARCH_CODE", "src/alpha.py", "abc123", "10| def Alpha():")
        hit_b = _code_row("SEARCH_CODE", "src/alpha.py", "abc123", "11| def Beta():")
        iid = uuid4()
        hyp = Hypothesis(
            tenant_id="t",
            investigation_id=iid,
            title="t",
            description="d",
            statement="Two definitions",
            status=HypothesisStatus.PROPOSED,
        )
        mk = lambda row, sym: HypothesisEvidenceAssessment(
            hypothesis_id=hyp.id,
            evidence_id=row.evidence_id,
            assessment=AssessmentType.SUPPORTS,
            strength=AssessmentStrength.STRONG,
            reason="r",
            claim_kind=ClaimKind.DEFINITION,
            claim_target=ClaimTarget(symbol=sym, file_path="src/alpha.py"),
        )
        hyp = hyp.model_copy(
            update={
                "assessments": [mk(hit_a, "Alpha"), mk(hit_b, "Beta")],
                "supporting_evidence_ids": [hit_a.evidence_id, hit_b.evidence_id],
            }
        )
        engine = VerificationEngine(claim_binding_version="cbv2")
        rebound, repairs = engine.repair_failed_groups(hyp, [hit_a, hit_b, donor])
        assert len(repairs) == 2
        assert {r.cited_id for r in repairs} == {str(hit_a.evidence_id), str(hit_b.evidence_id)}
        assert {r.donor_id for r in repairs} == {str(donor.evidence_id)}
        assert {r.claim_key for r in repairs} == {
            "Two definitions|DEFINITION",
        } or len({r.target.get("symbol") for r in repairs}) == 2
        assert {r.target.get("symbol") for r in repairs} == {"Alpha", "Beta"}

    def test_conflicting_same_citation_donors_fail_closed(self):
        hit = _def_hit()
        donor_a = _def_read()
        donor_b = _def_read()
        repairs = [
            RepairedBinding(
                claim_ref="a",
                claim_key="s|DEFINITION",
                target_ref="a-target",
                target={"symbol": "Alpha"},
                cited_id=str(hit.evidence_id),
                donor_id=str(donor_a.evidence_id),
                basis="snippet_containment",
                outcome="applied",
                binding_version="cbv3",
            ),
            RepairedBinding(
                claim_ref="b",
                claim_key="s|DEFINITION",
                target_ref="b-target",
                target={"symbol": "Beta"},
                cited_id=str(hit.evidence_id),
                donor_id=str(donor_b.evidence_id),
                basis="snippet_containment",
                outcome="applied",
                binding_version="cbv3",
            ),
        ]
        engine = VerificationEngine(claim_binding_version="cbv3")
        assert engine._repair_mapping(repairs) is None


# ---------------------------------------------------------------------------
# HISTORY strictness under cbv2/cbv3
# ---------------------------------------------------------------------------


def _commit_row(path, sha, subject, summary_snippet):
    iid = uuid4()
    now = datetime.now(UTC)
    return Evidence(
        tenant_id="t",
        investigation_id=iid,
        evidence_type=EvidenceType.COMMIT_HISTORY,
        provider="git",
        source="test://git",
        title="t",
        summary=summary_snippet,
        content_snippet=summary_snippet,
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
                provider_type="test", operation="GET_HISTORY", normalized_query_hash="h"
            ),
            source_location=SourceLocation(
                system="git", identifier="x", file_path=path, revision=sha
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=f"fp-{uuid4()}",
        attributes={},
    )


class TestHistoryStrictness:
    def test_lax_only_donor_never_repairs(self):
        # Donor passes the lax path (same file@rev) but fails the strict
        # subject rule: no repair under cbv2 or cbv3.
        target = ClaimTarget(
            file_path="src/a.py", commit_id="abc123", commit_subject="Tighten window"
        )
        candidate = _commit_row("src/a.py", "abc123", "Tighten window", "Tighten window")
        lax_donor = _commit_row("src/a.py", "abc123", "Other work", "Tighten window and more")
        assert repair_citation("HISTORY", target, candidate, lax_donor, "cbv2") is False
        assert repair_citation("HISTORY", target, candidate, lax_donor, "cbv3") is False
        # Contrast: the lax legacy path still accepts the same donor.
        assert repair_citation("HISTORY", target, candidate, lax_donor, "cbv1") is True

    def test_locator_qualified_vs_repo_relative_repairs(self):
        target = _def_target("org/repo/src/alpha.py")
        hit = _def_hit(path="org/repo/src/alpha.py")
        donor = _def_read(path="src/alpha.py")
        assert _paths_match_for_repair(hit, donor) is True
        assert repair_citation("DEFINITION", target, hit, donor) is True


# ---------------------------------------------------------------------------
# Engine end to end (site 1)
# ---------------------------------------------------------------------------


class TestEngineEndToEnd:
    def _run(self, pin, items, bank):
        hit = items[0]
        hyp = _def_hyp(hit)
        engine = VerificationEngine(claim_binding_version=pin)
        result = engine.evaluate_hypothesis(
            hyp,
            items,
            [],
            [],
            _policy(),
            evidence_bank=bank,
        )
        return engine, hyp, result

    def test_cbv1_baseline_no_repair(self):
        hit, donor = _def_hit(), _def_read()
        engine, _, result = self._run("cbv1", [hit], [hit, donor])
        assert result.status == VerificationStatus.INCONCLUSIVE
        assert list(getattr(result.diagnostics, "repaired", None) or []) == []
        assert engine._repair_calls == 0

    def test_cbv2_shadow_parity(self):
        hit, donor = _def_hit(), _def_read()
        engine1, _, base = self._run("cbv1", [hit], [hit, donor])
        engine2, _, result = self._run("cbv2", [hit], [hit, donor])
        assert result.status == base.status == VerificationStatus.INCONCLUSIVE
        assert str(result.corroborating_evidence_ids or []) == str(
            base.corroborating_evidence_ids or []
        )
        repaired = list(getattr(result.diagnostics, "repaired", None) or [])
        assert len(repaired) == 1
        assert repaired[0].outcome == "shadow"
        assert repaired[0].binding_version == "cbv2"
        assert repaired[0].donor_id == str(donor.evidence_id)
        # No limitation on a shadow record.
        codes = [
            str(getattr(lim.code, "value", lim.code) or "") for lim in (result.limitations or [])
        ]
        assert LimitationCode.REPAIRED_CITATION.value not in codes

    def test_cbv3_applied(self):
        hit, donor = _def_hit(), _def_read()
        engine, _, result = self._run("cbv3", [hit], [hit, donor])
        repaired = list(getattr(result.diagnostics, "repaired", None) or [])
        assert len(repaired) == 1
        assert repaired[0].outcome == "applied"
        assert repaired[0].binding_version == "cbv3"
        if result.status == VerificationStatus.VERIFIED:
            codes = [
                str(getattr(lim.code, "value", lim.code) or "")
                for lim in (result.limitations or [])
            ]
            assert LimitationCode.REPAIRED_CITATION.value in codes


class TestRevalidateFailedProofNone:
    """Finding D: HISTORY/location validators return None both when
    inapplicable and when the proof fails on cited rows. A failed proof
    must reach repair; other None shapes must not broaden the trigger.
    These tests use the REAL validators, unlike the stub-based class."""

    def _history_hyp(self, cited_id):
        iid = uuid4()
        hyp = Hypothesis(
            tenant_id="t",
            investigation_id=iid,
            title="filter_orders changed recently",
            description="filter_orders changed recently",
            statement="filter_orders changed recently",
            status=HypothesisStatus.PROPOSED,
            supporting_evidence_ids=[cited_id],
        )
        asm = HypothesisEvidenceAssessment(
            hypothesis_id=hyp.id,
            evidence_id=cited_id,
            assessment=AssessmentType.SUPPORTS,
            strength=AssessmentStrength.STRONG,
            reason="recent change",
            claim_kind=ClaimKind.HISTORY,
            claim_target=ClaimTarget(
                file_path="src/orders.py",
                commit_id="abc123",
                commit_subject="Fix filter edge case",
            ),
        )
        return hyp.model_copy(update={"assessments": [asm]})

    def test_failed_history_proof_repairs_under_cbv2(self):
        # Cited row matches file+commit but not the structured subject:
        # the real validator returns None (failed proof, not inapplicable)
        # and repair must still engage.
        cited = _commit_row("src/orders.py", "abc123", "Unrelated change", "Unrelated change")
        donor = _commit_row(
            "src/orders.py", "abc123", "Fix filter edge case", "Fix filter edge case"
        )
        # Containment for the donor match: the donor snippet must show (at
        # least) what the cited row showed, while the cited *subject*
        # mismatches the structured claim subject (the failed proof).
        cited = cited.model_copy(update={"content_snippet": "Fix filter"})
        hyp = self._history_hyp(cited.evidence_id)
        engine = VerificationEngine(claim_binding_version="cbv2")
        assert engine.validate_history_claim(hyp, [cited], []) is None
        result, repairs = engine.revalidate_with_repair(
            lambda h, w: engine.validate_history_claim(h, w, []),
            hyp,
            [cited],
            [cited, donor],
            [],
            None,
            None,
            (ClaimKind.HISTORY,),
        )
        assert result is None
        assert len(repairs) == 1 and repairs[0].outcome == "shadow"
        assert repairs[0].donor_id == str(donor.evidence_id)

    def test_inapplicable_kind_stays_silent(self):
        # BEHAVIOR hypothesis: the history validator is inapplicable
        # (kind mismatch), so even with cited rows no repair is attempted.
        cited = _commit_row("src/orders.py", "abc123", "Unrelated change", "Unrelated change")
        iid = uuid4()
        hyp = Hypothesis(
            tenant_id="t",
            investigation_id=iid,
            title="s",
            description="s",
            statement="throughput drops under load",
            status=HypothesisStatus.PROPOSED,
            supporting_evidence_ids=[cited.evidence_id],
        )
        asm = HypothesisEvidenceAssessment(
            hypothesis_id=hyp.id,
            evidence_id=cited.evidence_id,
            assessment=AssessmentType.SUPPORTS,
            strength=AssessmentStrength.STRONG,
            reason="behavior",
            claim_kind=ClaimKind.BEHAVIOR,
            claim_target=ClaimTarget(symbol="filter_orders"),
        )
        hyp = hyp.model_copy(update={"assessments": [asm]})
        engine = VerificationEngine(claim_binding_version="cbv2")
        assert engine.validate_history_claim(hyp, [cited], []) is None
        result, repairs = engine.revalidate_with_repair(
            lambda h, w: engine.validate_history_claim(h, w, []),
            hyp,
            [cited],
            [cited],
            [],
            None,
            None,
            (ClaimKind.HISTORY,),
        )
        assert result is None
        assert repairs == []

    def test_uncited_claim_stays_silent(self):
        # No cited rows: nothing to rebind, no repair even for HISTORY.
        hyp = self._history_hyp(uuid4())
        hyp = hyp.model_copy(update={"supporting_evidence_ids": []})
        asm = hyp.assessments[0].model_copy(update={"evidence_id": None})
        hyp = hyp.model_copy(update={"assessments": [asm]})
        engine = VerificationEngine(claim_binding_version="cbv2")
        result, repairs = engine.revalidate_with_repair(
            lambda h, w: engine.validate_history_claim(h, w, []),
            hyp,
            [],
            [],
            [],
            None,
            None,
            (ClaimKind.HISTORY,),
        )
        assert result is None
        assert repairs == []

    def test_legacy_none_without_expect_kinds(self):
        # No expect_kinds: legacy behavior preserved (None exits).
        cited = _commit_row("src/orders.py", "abc123", "Unrelated change", "Unrelated change")
        hyp = self._history_hyp(cited.evidence_id)
        engine = VerificationEngine(claim_binding_version="cbv2")
        result, repairs = engine.revalidate_with_repair(
            lambda h, w: engine.validate_history_claim(h, w, []),
            hyp,
            [cited],
            [cited],
        )
        assert result is None
        assert repairs == []

    def test_ghost_never_repairs(self):
        ghost = uuid4()
        hit, donor = _def_hit(), _def_read()
        hyp = _def_hyp(hit).model_copy(
            update={
                "supporting_evidence_ids": [ghost],
                "assessments": [
                    HypothesisEvidenceAssessment(
                        hypothesis_id=uuid4(),
                        evidence_id=ghost,
                        assessment=AssessmentType.SUPPORTS,
                        strength=AssessmentStrength.STRONG,
                        reason="r",
                        claim_kind=ClaimKind.DEFINITION,
                        claim_target=_def_target(),
                    )
                ],
            }
        )
        engine = VerificationEngine(claim_binding_version="cbv3")
        result = engine.evaluate_hypothesis(hyp, [hit, donor], [], [], _policy())
        assert result.status == VerificationStatus.INCONCLUSIVE
        assert list(getattr(result.diagnostics, "repaired", None) or []) == []


# ---------------------------------------------------------------------------
# revalidate_with_repair (fast-path wrap mechanics)
# ---------------------------------------------------------------------------


class TestRevalidateWithRepair:
    def _stub(self, hit_id, donor_id, gate_rows):
        # Mimics a fast-path validate: INCONCLUSIVE+empty when gate fails
        # on the cited rows, VERIFIED with donor corroborating otherwise.
        def run(hypothesis, working):
            from investigation_agent_platform.application.investigation.verification import (
                VerificationEngine as VE,
            )

            cited = [str(r) for r in (getattr(hypothesis, "supporting_evidence_ids", []) or [])]
            target = _def_target()
            rows = [r for r in working if str(getattr(r, "evidence_id", "")) in set(cited)]
            ok, _ = VE.definition_window_proof(rows or working, target)
            if ok:
                return VerificationResult(
                    hypothesis_id=hypothesis.id,
                    status=VerificationStatus.VERIFIED,
                    corroborating_evidence_ids=[donor_id],
                    explanation="grounded",
                    confidence=_policy().__class__
                    and __import__(
                        "investigation_agent_platform.application.investigation.verification",
                        fromlist=["InvestigationConfidence"],
                    ).InvestigationConfidence(
                        coverage_score=1.0,
                        reliability_score=1.0,
                        causal_score=1.0,
                        contradiction_penalty=0.0,
                    ),
                )
            return VerificationResult(
                hypothesis_id=hypothesis.id,
                status=VerificationStatus.INCONCLUSIVE,
                corroborating_evidence_ids=[],
                explanation="gate failed",
                confidence=__import__(
                    "investigation_agent_platform.application.investigation.verification",
                    fromlist=["InvestigationConfidence"],
                ).InvestigationConfidence(
                    coverage_score=0.0,
                    reliability_score=0.0,
                    causal_score=0.0,
                    contradiction_penalty=0.0,
                ),
            )

        return run

    def test_cbv1_verbatim(self):
        hit, donor = _def_hit(), _def_read()
        hyp = _def_hyp(hit)
        engine = VerificationEngine(claim_binding_version="cbv1")
        result, repairs = engine.revalidate_with_repair(
            self._stub(hit.evidence_id, donor.evidence_id, None),
            hyp,
            [hit],
            [hit, donor],
        )
        assert result.status == VerificationStatus.INCONCLUSIVE
        assert repairs == []

    def test_cbv2_shadow_no_rerun(self):
        hit, donor = _def_hit(), _def_read()
        hyp = _def_hyp(hit)
        engine = VerificationEngine(claim_binding_version="cbv2")
        enf = engine.claim_binding_enforcement(hyp, [hit, donor], None)
        result, repairs = engine.revalidate_with_repair(
            self._stub(hit.evidence_id, donor.evidence_id, None),
            hyp,
            [hit],
            [hit, donor],
            None,
            None,
            enf,
        )
        assert result.status == VerificationStatus.INCONCLUSIVE
        assert len(repairs) == 1 and repairs[0].outcome == "shadow"

    def test_cbv3_single_rerun_applies(self):
        hit, donor = _def_hit(), _def_read()
        hyp = _def_hyp(hit)
        engine = VerificationEngine(claim_binding_version="cbv3")
        enf = engine.claim_binding_enforcement(hyp, [hit, donor], None)
        result, repairs = engine.revalidate_with_repair(
            self._stub(hit.evidence_id, donor.evidence_id, None),
            hyp,
            [hit],
            [hit, donor],
            None,
            None,
            enf,
        )
        assert result.status == VerificationStatus.VERIFIED
        assert len(repairs) == 1 and repairs[0].outcome == "applied"
        codes = [
            str(getattr(lim.code, "value", lim.code) or "") for lim in (result.limitations or [])
        ]
        assert LimitationCode.REPAIRED_CITATION.value in codes


# ---------------------------------------------------------------------------
# enforcement_with_repairs + attach
# ---------------------------------------------------------------------------


class TestEnforcementAndAttach:
    def test_enforcement_with_repairs(self):
        enf = ClaimBindingEnforcement(
            cited_ids=["c1"],
            kept_ids=["c1"],
            cited_kept_count=1,
        )
        out = VerificationEngine.enforcement_with_repairs(enf, ["d1"])
        assert "d1" in out.kept_ids
        assert out.repaired_ids == ["d1"]
        assert out.cited_kept_count == 2
        # Input unchanged.
        assert enf.repaired_ids == []

    def test_attach_repairs(self):
        hit, donor = _def_hit(), _def_read()
        from investigation_agent_platform.application.evidence.retrieval_ledger import (
            claim_key,
        )

        ck = claim_key("The request handler is defined", "DEFINITION")
        _, repairs = attempt_claim_repair(
            "DEFINITION",
            ck,
            _def_target(),
            [str(hit.evidence_id)],
            {str(hit.evidence_id)},
            {str(hit.evidence_id): hit, str(donor.evidence_id): donor},
            "cbv2",
            "r",
        )
        base = VerificationResult(
            hypothesis_id=uuid4(),
            status=VerificationStatus.INCONCLUSIVE,
            corroborating_evidence_ids=[],
            explanation="x",
            confidence=__import__(
                "investigation_agent_platform.application.investigation.verification",
                fromlist=["InvestigationConfidence"],
            ).InvestigationConfidence(
                coverage_score=0.0,
                reliability_score=0.0,
                causal_score=0.0,
                contradiction_penalty=0.0,
            ),
        )
        out = VerificationEngine.attach_repairs(base, repairs)
        assert len(list(getattr(out.diagnostics, "repaired", None) or [])) == 1
        # Idempotent.
        out2 = VerificationEngine.attach_repairs(out, repairs)
        assert len(list(getattr(out2.diagnostics, "repaired", None) or [])) == 1


# ---------------------------------------------------------------------------
# Pin matrix (D3)
# ---------------------------------------------------------------------------


class TestPinMatrix:
    def test_resolver_accepts_cbv3(self):
        assert resolve_claim_binding_version({"claim_binding_version": "cbv3"}) == (
            CLAIM_BINDING_CBV3,
            "pinned",
        )
        assert CLAIM_BINDING_CURRENT == "cbv1"
        assert KNOWN_CLAIM_BINDING_VERSIONS == {"cbv1", "cbv2", "cbv3"}

    def test_engine_accepts_cbv3_rejects_unknown(self):
        assert VerificationEngine(claim_binding_version="cbv3").claim_binding_version == "cbv3"
        with pytest.raises(UnknownVerifierVersion):
            VerificationEngine(claim_binding_version="cbv9")
        with pytest.raises(VerifierVersionUnavailable):
            resolve_claim_binding_version(None)

    def test_creation_paths_stamp_cbv1_only(self):
        import pathlib

        roots = {
            "src/investigation_agent_platform/application/knowledge/intake.py": None,
            "src/investigation_agent_platform/api/dev_seed.py": None,
            "src/investigation_agent_platform/application/investigation/services.py": None,
        }
        for rel in roots:
            text = pathlib.Path(rel).read_text()
            assert '"cbv2"' not in text and "'cbv2'" not in text, rel
            assert '"cbv3"' not in text and "'cbv3'" not in text, rel
            # Stamps use the CLAIM_BINDING_CURRENT constant (or the CLAIM_BINDING_CBV1
            # constant, or the literal).
            assert "CLAIM_BINDING_CURRENT" in text or "CLAIM_BINDING_CBV1" in text or '"cbv1"' in text or "'cbv1'" in text, rel

    def test_no_bare_cbv2_equality_gates_remain(self):
        import pathlib

        hits = []
        for path in pathlib.Path("src/investigation_agent_platform").rglob("*.py"):
            try:
                text = path.read_text()
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if "claim_binding_version == CLAIM_BINDING_CBV2" in line:
                    hits.append(f"{path}:{i}")
        assert hits == [], f"bare cbv2 equality gates remain: {hits}"


# ---------------------------------------------------------------------------
# D6: no ungated repair (updated pin test)
# ---------------------------------------------------------------------------


class TestNoUngatedRepair:
    def test_repair_callers_enumerated_and_gated(self):
        import pathlib

        allowed = {
            "src/investigation_agent_platform/application/investigation/verification.py",
            "src/investigation_agent_platform/application/worker/activities/verification.py",
            "src/investigation_agent_platform/application/worker/activities/lifecycle.py",
        }
        hits = []
        for path in pathlib.Path("src/investigation_agent_platform").rglob("*.py"):
            rel = str(path)
            if rel in allowed:
                continue
            try:
                text = path.read_text()
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if "repair_citation" in line or "attempt_claim_repair" in line:
                    hits.append(f"{path}:{i}")
        assert hits == [], f"repair helpers referenced outside gated sites: {hits}"

    def test_cbv1_executes_zero_repair_code(self):
        # Behavioral half of the pin test: absence of records plus a zero
        # execution counter (absence alone also matches a skipped path).
        hit = _def_hit()
        hyp = _def_hyp(hit)
        engine = VerificationEngine(claim_binding_version="cbv1")
        result = engine.evaluate_hypothesis(hyp, [hit], [], [], _policy())
        assert engine._repair_calls == 0
        assert list(getattr(result.diagnostics, "repaired", None) or []) == []

    def test_orientation_only_vetoes_repair(self):
        from investigation_agent_platform.application.evidence.code_recall import (
            FUZZY_ORIENTATION_ATTR,
        )

        hit, donor = _def_hit(), _def_read()
        orientation_hit = hit.model_copy(update={"attributes": {FUZZY_ORIENTATION_ATTR: True}})
        hyp = _def_hyp(orientation_hit)
        engine = VerificationEngine(claim_binding_version="cbv3")
        rebound, repairs = engine.repair_failed_groups(hyp, [orientation_hit, donor])
        assert rebound == [] and repairs == []
        assert engine._repair_calls == 0


# ---------------------------------------------------------------------------
# Telemetry, markers, battery
# ---------------------------------------------------------------------------


class TestTelemetryAndMarkers:
    def test_port_has_repair_method(self):
        from investigation_agent_platform.ports.observability.telemetry import (
            ObservabilityPort,
        )

        assert hasattr(ObservabilityPort, "record_claim_repair")

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
        disabled.record_claim_repair(ctx, "DEFINITION", "snippet_containment", "shadow", "cbv2")
        enabled = OpenTelemetryObservabilityAdapter(
            service_name="test", otlp_endpoint="", enabled=True
        )
        enabled.record_claim_repair(ctx, "DEFINITION", "snippet_containment", "applied", "cbv3")
        enabled.record_claim_repair(ctx, "evil", "evil", "evil", "v99")
        assert enabled._closed("applied", enabled._REPAIR_OUTCOMES, "x") == "applied"
        assert enabled._closed("nope", enabled._REPAIR_OUTCOMES, "fallback") == "fallback"

    def test_limitation_kind_maps_repaired(self):
        from investigation_agent_platform.application.worker.activities.common import (
            _KNOWN_LIMITATION_CODES,
            _limitation_kind,
        )

        assert "REPAIRED_CITATION" in _KNOWN_LIMITATION_CODES
        assert _limitation_kind("REPAIRED_CITATION: rebound") == "REPAIRED_CITATION"

    def test_emit_verify_telemetry_repair_params(self):
        from investigation_agent_platform.application.worker.activities.common import (
            _emit_verify_telemetry,
        )

        calls: list[tuple] = []

        class _Obs:
            def record_claim_repair(self, ctx, kind, basis, outcome, version):
                calls.append(("repair", kind, basis, outcome, version))

            def __getattr__(self, name):
                def _noop(*args, **kwargs):
                    return None

                return _noop

        from investigation_agent_platform.application.investigation.verification import (
            VerificationDiagnostics,
        )

        hit, donor = _def_hit(), _def_read()
        from investigation_agent_platform.application.evidence.retrieval_ledger import (
            claim_key,
        )

        ck = claim_key("The request handler is defined", "DEFINITION")
        _, repairs = attempt_claim_repair(
            "DEFINITION",
            ck,
            _def_target(),
            [str(hit.evidence_id)],
            {str(hit.evidence_id)},
            {str(hit.evidence_id): hit, str(donor.evidence_id): donor},
            "cbv2",
            "r",
        )
        hyp = _def_hyp(hit)
        diag = VerificationDiagnostics(repaired=repairs)
        result = VerificationResult(
            hypothesis_id=uuid4(),
            status=VerificationStatus.INCONCLUSIVE,
            corroborating_evidence_ids=[],
            explanation="x",
            confidence=__import__(
                "investigation_agent_platform.application.investigation.verification",
                fromlist=["InvestigationConfidence"],
            ).InvestigationConfidence(
                coverage_score=0.0,
                reliability_score=0.0,
                causal_score=0.0,
                contradiction_penalty=0.0,
            ),
            diagnostics=diag,
        )
        ctx = SimpleNamespace(observability=_Obs())
        _emit_verify_telemetry(
            ctx, "t", uuid4(), hyp, result, [], "cbv2", "pinned", [], [hit, donor]
        )
        assert ("repair", "DEFINITION", "snippet_containment", "shadow", "cbv2") in calls

    def test_marker_serialization_round_trip(self):
        from investigation_agent_platform.application.investigation.verification import (
            VerificationDiagnostics,
        )

        hit, donor = _def_hit(), _def_read()
        from investigation_agent_platform.application.evidence.retrieval_ledger import (
            claim_key,
        )

        ck = claim_key("The request handler is defined", "DEFINITION")
        _, repairs = attempt_claim_repair(
            "DEFINITION",
            ck,
            _def_target(),
            [str(hit.evidence_id)],
            {str(hit.evidence_id)},
            {str(hit.evidence_id): hit, str(donor.evidence_id): donor},
            "cbv3",
            "r",
        )
        diag = VerificationDiagnostics(repaired=repairs)
        attrs = build_verify_evaluation_attributes(
            diag,
            turn=1,
            hypothesis_id=uuid4(),
            verifier_version="v2",
        )
        assert attrs is not None
        assert len(attrs["repaired"]) == 1
        assert attrs["repaired"][0]["outcome"] == "applied"
        assert attrs["repaired"][0]["target"]["symbol"] == "handleRequest"
        assert attrs["repaired_count"] == 1
        assert attrs["repair_shadow_count"] == 0
        assert attrs["repaired_truncated"] is False

    def test_marker_truncation_fails_closed(self):
        from investigation_agent_platform.application.investigation.verification import (
            VerificationDiagnostics,
        )

        reps = [
            RepairedBinding(
                claim_ref=f"{i:016x}",
                cited_id=f"c{i}",
                donor_id=f"d{i}",
                basis="snippet_containment",
                outcome="shadow" if i % 2 else "applied",
                binding_version="cbv2" if i % 2 else "cbv3",
            )
            for i in range(11)
        ]
        diag = VerificationDiagnostics(repaired=reps)
        attrs = build_verify_evaluation_attributes(
            diag,
            turn=1,
            hypothesis_id=uuid4(),
            verifier_version="v2",
        )
        assert attrs is not None
        assert len(attrs["repaired"]) == 10
        assert attrs["repaired_truncated"] is True
        assert attrs["repaired_count"] + attrs["repair_shadow_count"] == 11
        assert len(attrs["repaired"]) != (attrs["repaired_count"] + attrs["repair_shadow_count"])

    def test_diagnostics_do_not_drop_repairs_before_marker_cap(self):
        reps = [
            RepairedBinding(
                claim_ref=f"{i:016x}",
                cited_id=f"c{i}",
                donor_id=f"d{i}",
                basis="snippet_containment",
                outcome="shadow",
                binding_version="cbv2",
            )
            for i in range(31)
        ]
        result = VerificationResult(
            hypothesis_id=uuid4(),
            status=VerificationStatus.INCONCLUSIVE,
            corroborating_evidence_ids=[],
            explanation="x",
            confidence=__import__(
                "investigation_agent_platform.application.investigation.verification",
                fromlist=["InvestigationConfidence"],
            ).InvestigationConfidence(
                coverage_score=0.0,
                reliability_score=0.0,
                causal_score=0.0,
                contradiction_penalty=0.0,
            ),
        )
        result = VerificationEngine.attach_repairs(result, reps)
        attrs = build_verify_evaluation_attributes(
            result.diagnostics,
            turn=1,
            hypothesis_id=result.hypothesis_id,
            verifier_version="v2",
        )
        assert attrs is not None
        assert len(result.diagnostics.repaired) == 31
        assert len(attrs["repaired"]) == 10
        assert attrs["repaired_truncated"] is True
        assert attrs["repair_shadow_count"] == 31

    def test_skeleton_defaults_repair_free(self):
        from investigation_agent_platform.application.investigation.verification import (
            VerificationDiagnostics,
        )

        attrs = build_verify_evaluation_attributes(
            VerificationDiagnostics(),
            turn=1,
            hypothesis_id=uuid4(),
            verifier_version="v2",
        )
        assert attrs is not None
        assert attrs["repaired"] == []
        assert attrs["repaired_truncated"] is False
        assert attrs["repaired_count"] == 0
        assert attrs["repair_shadow_count"] == 0

    def test_shadow_repair_review(self):
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "scripts" / "retrieval_benefit.py"
        spec = importlib.util.spec_from_file_location("retrieval_benefit", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        shadow_repair_review = module.shadow_repair_review

        evals = {
            "inv-1": [
                {
                    "turn": 2,
                    "hypothesis_id": "h1",
                    "repaired": [
                        {
                            "claim_key": "s|DEFINITION",
                            "claim_ref": "r1",
                            "target_ref": "t1",
                            "target": {"symbol": "S", "file_path": "f.py"},
                            "cited_id": "c1",
                            "donor_id": "d1",
                            "basis": "snippet_containment",
                            "outcome": "shadow",
                            "binding_version": "cbv2",
                            "gate_reason": "g",
                        }
                    ],
                    "repaired_truncated": False,
                    "repaired_count": 0,
                    "repair_shadow_count": 1,
                },
                {
                    "turn": 3,
                    "hypothesis_id": "h1",
                    "repaired": [{"claim_ref": "r2"}],
                    "repaired_truncated": True,
                    "repaired_count": 0,
                    "repair_shadow_count": 5,
                },
            ]
        }
        review = shadow_repair_review(evals)
        assert review["n_candidates"] == 2
        assert review["complete"] is False
        assert len(review["truncated_markers"]) == 1
        assert len(review["count_mismatch_markers"]) == 1
        first = review["candidates"][0]
        assert first["disposition_key"] == "h1|2|r1|c1|d1|cbv2"
        assert first["target"] == {"symbol": "S", "file_path": "f.py"}
        clean = shadow_repair_review({"inv-1": [dict(evals["inv-1"][0])]})
        assert clean["complete"] is True

    def test_complete_does_not_mean_reviewed(self):
        # Finding E guardrail: marker integrity (complete) must not be
        # read as review coverage. Default dispositions leave reviewed
        # False even on a clean marker.
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "scripts" / "retrieval_benefit.py"
        spec = importlib.util.spec_from_file_location("retrieval_benefit", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        shadow_repair_review = module.shadow_repair_review

        evals = {
            "inv-1": [
                {
                    "turn": 2,
                    "hypothesis_id": "h1",
                    "repaired": [
                        {
                            "claim_key": "s|DEFINITION",
                            "claim_ref": "r1",
                            "target_ref": "t1",
                            "cited_id": "c1",
                            "donor_id": "d1",
                            "basis": "snippet_containment",
                            "outcome": "shadow",
                            "binding_version": "cbv2",
                            "gate_reason": "g",
                        }
                    ],
                    "repaired_truncated": False,
                    "repaired_count": 0,
                    "repair_shadow_count": 1,
                }
            ]
        }
        review = shadow_repair_review(evals)
        assert review["complete"] is True
        assert review["reviewed"] is False
        assert review["unreviewed_count"] == 1
        assert review["candidates"][0]["disposition"] == "unreviewed"
        key = review["candidates"][0]["disposition_key"]
        approved = shadow_repair_review(evals, {key: "approved"})
        assert approved["reviewed"] is True
        assert approved["unreviewed_count"] == 0
        assert approved["candidates"][0]["disposition"] == "approved"
        empty = shadow_repair_review({"inv-1": []})
        assert empty["reviewed"] is False

    def test_review_carries_donor_snippet_and_renders_it(self):
        # Finding G: the review input is row content, never a bare ID —
        # the loader carries donor_snippet and the human-readable report
        # renders each unreviewed candidate with its snippet excerpt.
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "scripts" / "retrieval_benefit.py"
        spec = importlib.util.spec_from_file_location("retrieval_benefit_g", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        evals = {
            "inv-1": [
                {
                    "turn": 2,
                    "hypothesis_id": "h1",
                    "repaired": [
                        {
                            "claim_key": "s|DEFINITION",
                            "claim_ref": "r1",
                            "target_ref": "t1",
                            "target": {"symbol": "S", "file_path": "f.py"},
                            "cited_id": "c1",
                            "donor_id": "d1",
                            "basis": "snippet_containment",
                            "outcome": "shadow",
                            "binding_version": "cbv2",
                            "gate_reason": "g",
                            "donor_snippet": "40| def handleRequest(req):",
                        }
                    ],
                    "repaired_truncated": False,
                    "repaired_count": 0,
                    "repair_shadow_count": 1,
                }
            ]
        }
        review = module.shadow_repair_review(evals)
        assert review["candidates"][0]["donor_snippet"] == "40| def handleRequest(req):"
        text = module.format_report(
            {
                "n_runs": 1,
                "evaluable_runs": 1,
                "n_attempts": 0,
                "n_claims": 1,
                "n_evaporation_events": 0,
                "n_missing_obligations": 0,
                "coverage": {},
                "attribution": {},
                "recovery": {},
                "truncation": {"flags": [], "truncation_seen": False, "applicable": True},
                "verdict_guard": {},
                "shadow_repair_review": review,
                "branch_recovery": False,
                "branch_diagnostic": False,
                "inconclusive": True,
                "pass": False,
            }
        )
        assert "review: claim_ref=r1 cited=c1 donor=d1" in text
        assert "40| def handleRequest(req):" in text


# ---------------------------------------------------------------------------
# Finding L: opinion-only early-skip through the real verify activity
# ---------------------------------------------------------------------------


class TestVerifyActivityNotCheckableEarlySkip:
    def _activity_ctx(self, inv_metadata, hyp, rows, events):
        from unittest.mock import AsyncMock

        from investigation_agent_platform.api.dependencies import (
            AppContext,
            set_app_context,
        )

        ctx = AppContext()
        ctx.investigation_repo = SimpleNamespace(
            get_by_id=AsyncMock(
                return_value=SimpleNamespace(metadata=dict(inv_metadata), application_id="app")
            )
        )
        ctx.evidence_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=list(rows))
        )
        ctx.hypothesis_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[hyp]),
            save=AsyncMock(),
        )
        ctx.timeline_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=list(events)),
            append=AsyncMock(return_value=None),
        )
        set_app_context(ctx)
        return ctx

    def _nc_hyp(self, inv_id):
        from investigation_agent_platform.domain.hypothesis.models import (
            AssessmentStrength,
            AssessmentType,
            ClaimKind,
            ClaimTarget,
            Hypothesis,
            HypothesisEvidenceAssessment,
            HypothesisStatus,
        )

        hyp = Hypothesis(
            tenant_id="t",
            investigation_id=inv_id,
            title="Editors are better than IDEs"[:256],
            description="Editors are better than IDEs",
            statement="Editors are better than IDEs",
            status=HypothesisStatus.PROPOSED,
        )
        asm = HypothesisEvidenceAssessment(
            hypothesis_id=hyp.id,
            evidence_id=None,
            assessment=AssessmentType.NEUTRAL,
            strength=AssessmentStrength.WEAK,
            reason="taste judgment",
            claim_kind=ClaimKind.NOT_CHECKABLE,
            claim_target=ClaimTarget(),
        )
        return hyp.model_copy(update={"assessments": [asm], "supporting_evidence_ids": []})

    @pytest.mark.asyncio
    async def test_all_nc_skips_activity_validators(self):
        from unittest.mock import patch

        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )
        from investigation_agent_platform.application.worker.activities import (
            VerifyRootCauseInput,
            verify_root_cause_activity,
        )

        inv_id = uuid4()
        hyp = self._nc_hyp(inv_id)
        self._activity_ctx(
            {"claim_binding_version": "cbv3", "verifier_version": "v2"},
            hyp,
            [],
            [],
        )
        with (
            patch.object(
                VerificationEngine,
                "validate_code_value_claim",
                side_effect=AssertionError("must not run on NC"),
            ),
            patch.object(
                VerificationEngine,
                "validate_history_claim",
                side_effect=AssertionError("must not run on NC"),
            ),
            patch.object(
                VerificationEngine,
                "validate_location_claim",
                side_effect=AssertionError("must not run on NC"),
            ),
            patch.object(
                VerificationEngine,
                "validate_doc_claim",
                side_effect=AssertionError("must not run on NC"),
            ),
            patch.object(
                VerificationEngine,
                "validate_state_claim",
                side_effect=AssertionError("must not run on NC"),
            ),
        ):
            result = await verify_root_cause_activity(
                VerifyRootCauseInput(tenant_id="t", investigation_id=str(inv_id))
            )
        assert result.success is True
        assert result.data.get("status") == "INCONCLUSIVE"
        assert result.data.get("verified") is False
        assert result.data.get("producing_path") == "general"


# ---------------------------------------------------------------------------
# Finding L: opinion-only early-skip through the real conclude gate
# ---------------------------------------------------------------------------


class TestConcludeNotCheckableEarlySkip:
    def _conclude_ctx(self, inv_metadata, hyp):
        from unittest.mock import AsyncMock

        from investigation_agent_platform.api.dependencies import (
            AppContext,
            set_app_context,
        )
        from investigation_agent_platform.domain.investigation.models import (
            InvestigationStatus,
        )

        saved: dict[str, Any] = {}

        async def _save_conclusion(tenant_id, conclusion):
            saved["conclusion"] = conclusion
            return conclusion

        ctx = AppContext()
        ctx.investigation_repo = SimpleNamespace(
            get_by_id=AsyncMock(
                return_value=SimpleNamespace(
                    metadata=dict(inv_metadata),
                    application_id="app",
                    status=InvestigationStatus.COMPLETED,
                )
            ),
            save=AsyncMock(),
        )
        ctx.evidence_repo = SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[]))
        ctx.hypothesis_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[hyp]),
            save=AsyncMock(),
        )
        ctx.finding_repo = SimpleNamespace(
            save_conclusion=AsyncMock(side_effect=_save_conclusion),
            save_finding=AsyncMock(return_value=None),
        )
        ctx.timeline_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[]),
            append=AsyncMock(return_value=None),
        )
        set_app_context(ctx)
        return ctx, saved

    @pytest.mark.asyncio
    async def test_all_nc_conclude_skips_validators(self):
        from unittest.mock import patch

        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )
        from investigation_agent_platform.application.worker.activities.inputs import (
            ConcludeInvestigationInput,
        )
        from investigation_agent_platform.application.worker.activities.lifecycle import (
            conclude_investigation_activity,
        )

        inv_id = uuid4()
        hyp = TestVerifyActivityNotCheckableEarlySkip()._nc_hyp(inv_id)
        _, saved = self._conclude_ctx(
            {"claim_binding_version": "cbv3", "verifier_version": "v2"},
            hyp,
        )
        with (
            patch.object(
                VerificationEngine,
                "validate_code_value_claim",
                side_effect=AssertionError("must not run on NC"),
            ),
            patch.object(
                VerificationEngine,
                "validate_history_claim",
                side_effect=AssertionError("must not run on NC"),
            ),
            patch.object(
                VerificationEngine,
                "validate_location_claim",
                side_effect=AssertionError("must not run on NC"),
            ),
            patch.object(
                VerificationEngine,
                "validate_doc_claim",
                side_effect=AssertionError("must not run on NC"),
            ),
            patch.object(
                VerificationEngine,
                "validate_state_claim",
                side_effect=AssertionError("must not run on NC"),
            ),
        ):
            result = await conclude_investigation_activity(
                ConcludeInvestigationInput(
                    tenant_id="t", investigation_id=str(inv_id), status="COMPLETED"
                )
            )
        assert result.success is True
        conclusion = saved.get("conclusion")
        assert conclusion is not None
        assert "NOT_CHECKABLE_SKIPPED" in list(getattr(conclusion, "limitations", None) or [])

    @pytest.mark.asyncio
    async def test_provider_blocked_conclude_persists_telemetry_deferred(self):
        # A provider-blocked run ends as a telemetry-deferred handoff, not
        # a FAILED platform error: INSUFFICIENT_EVIDENCE with a persisted
        # TELEMETRY_DEFERRED handoff and a resume-at-blocked-fetch check.
        from investigation_agent_platform.application.worker.activities.inputs import (
            ConcludeInvestigationInput,
        )
        from investigation_agent_platform.application.worker.activities.lifecycle import (
            conclude_investigation_activity,
        )
        from investigation_agent_platform.domain.finding.models import HandoffState
        from investigation_agent_platform.domain.investigation.models import (
            InvestigationStatus,
        )

        inv_id = uuid4()
        hyp = TestVerifyActivityNotCheckableEarlySkip()._nc_hyp(inv_id)
        _, saved = self._conclude_ctx(
            {"claim_binding_version": "cbv3", "verifier_version": "v2"},
            hyp,
        )
        result = await conclude_investigation_activity(
            ConcludeInvestigationInput(
                tenant_id="t",
                investigation_id=str(inv_id),
                status="FAILED",
                reason="provider_blocked:auth_failed",
            )
        )
        assert result.success is True
        assert result.data.get("status") == InvestigationStatus.COMPLETED.value
        conclusion = saved.get("conclusion")
        assert conclusion is not None
        assert str(getattr(conclusion, "status", "")) == "INSUFFICIENT_EVIDENCE"
        assert conclusion.handoff is not None
        assert conclusion.handoff.state == HandoffState.TELEMETRY_DEFERRED
        assert conclusion.handoff.version == "handoff-v1"
        assert "blocked provider fetch" in (conclusion.next_check or "")
        assert any(
            "blocked provider fetch" in step for step in (conclusion.recommended_next_steps or [])
        )


class TestVerifyActivityRepair:
    def _activity_ctx(self, inv_metadata, hyp, rows, events):
        from unittest.mock import AsyncMock

        from investigation_agent_platform.api.dependencies import (
            AppContext,
            set_app_context,
        )

        ctx = AppContext()
        ctx.investigation_repo = SimpleNamespace(
            get_by_id=AsyncMock(
                return_value=SimpleNamespace(metadata=dict(inv_metadata), application_id="app")
            )
        )
        ctx.evidence_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=list(rows))
        )
        ctx.hypothesis_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[hyp]),
            save=AsyncMock(),
        )
        ctx.timeline_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=list(events)),
            append=AsyncMock(return_value=None),
        )
        set_app_context(ctx)
        return ctx

    @pytest.mark.asyncio
    async def test_verify_activity_cbv2_shadow(self):
        from investigation_agent_platform.application.worker.activities import (
            VerifyRootCauseInput,
            verify_root_cause_activity,
        )
        from investigation_agent_platform.domain.hypothesis.models import (
            HypothesisCausalStep,
        )

        hit, donor = _def_hit(), _def_read()
        inv_id = uuid4()
        hit = hit.model_copy(update={"tenant_id": "t", "investigation_id": inv_id})
        donor = donor.model_copy(update={"tenant_id": "t", "investigation_id": inv_id})
        hyp = _def_hyp(hit).model_copy(
            update={
                "investigation_id": inv_id,
                "tenant_id": "t",
                # A grounded causal chain: without it the activity takes
                # the "blocked" branch (non-empty corroborating), which
                # the repair trigger correctly excludes.
                "causal_steps": [
                    HypothesisCausalStep(
                        cause="handler defined",
                        mechanism="definition",
                        effect="requests dispatch",
                        evidence_ids=[hit.evidence_id],
                    )
                ],
            }
        )
        self._activity_ctx(
            {"claim_binding_version": "cbv2", "verifier_version": "v2"},
            hyp,
            [hit, donor],
            [],
        )
        result = await verify_root_cause_activity(
            VerifyRootCauseInput(tenant_id="t", investigation_id=str(inv_id))
        )
        assert result.success is True
        assert result.data.get("status") == "INCONCLUSIVE"
        assert int(result.data.get("claim_binding_repair_shadow", 0)) >= 1
        assert int(result.data.get("claim_binding_repaired", 0)) == 0


# ---------------------------------------------------------------------------
# Finding M: no-candidate runs carry an explicit empty feed
# ---------------------------------------------------------------------------


class TestNoCandidateExplicitEmptyFeed:
    def _ctx(self, inv_metadata, hyp, rows):
        from unittest.mock import AsyncMock

        from investigation_agent_platform.api.dependencies import (
            AppContext,
            set_app_context,
        )

        ctx = AppContext()
        ctx.investigation_repo = SimpleNamespace(
            get_by_id=AsyncMock(
                return_value=SimpleNamespace(metadata=dict(inv_metadata), application_id="app")
            )
        )
        ctx.evidence_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=list(rows))
        )
        ctx.hypothesis_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[hyp]),
            save=AsyncMock(),
        )
        ctx.timeline_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[]),
            append=AsyncMock(return_value=None),
        )
        set_app_context(ctx)
        return ctx

    def _rejected_hyp(self, inv_id):
        from investigation_agent_platform.domain.hypothesis.models import (
            Hypothesis,
            HypothesisStatus,
        )

        return Hypothesis(
            tenant_id="t",
            investigation_id=inv_id,
            title="rejected"[:256],
            description="rejected",
            statement="rejected",
            status=HypothesisStatus.REJECTED,
        )

    @pytest.mark.asyncio
    async def test_no_candidate_current_carries_empty_feed(self):
        from investigation_agent_platform.application.worker.activities import (
            VerifyRootCauseInput,
            verify_root_cause_activity,
        )
        from investigation_agent_platform.application.worker.workflows.forcing import (
            _carry_gap_orders,
        )

        inv_id = uuid4()
        self._ctx(
            {"claim_binding_version": "cbv3", "verifier_version": "v2"},
            self._rejected_hyp(inv_id),
            [],
        )
        result = await verify_root_cause_activity(
            VerifyRootCauseInput(tenant_id="t", investigation_id=str(inv_id))
        )
        assert result.success is True
        assert result.data.get("producing_path") == "no-candidate"
        # Finding M: explicit empty feed, never a missing key — the
        # carrier writes a tombstone (supersedes stale carriers) instead
        # of the fault signal (None, no carrier).
        assert result.data.get("claim_gaps") == []
        carrier = _carry_gap_orders(dict(result.data), 3)
        assert carrier is not None
        assert carrier["kind"] == "work_order_feed"
        assert carrier["orders"] == []

    @pytest.mark.asyncio
    async def test_no_candidate_legacy_keeps_keyless_shape(self):
        from investigation_agent_platform.application.worker.activities import (
            VerifyRootCauseInput,
            verify_root_cause_activity,
        )

        inv_id = uuid4()
        self._ctx(
            {"claim_binding_version": "cbv1", "verifier_version": "v1"},
            self._rejected_hyp(inv_id),
            [],
        )
        result = await verify_root_cause_activity(
            VerifyRootCauseInput(tenant_id="t", investigation_id=str(inv_id))
        )
        assert result.success is True
        assert result.data.get("producing_path") == "no-candidate"
        assert "claim_gaps" not in dict(result.data)
