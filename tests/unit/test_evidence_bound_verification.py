# tests/unit/test_evidence_bound_verification.py
"""P0-3: ranked, evidence-bound verification with real contradictions/causality."""

from datetime import UTC, datetime
from uuid import uuid4

from investigation_agent_platform.application.investigation.verification import (
    CausalRelationship,
    ConclusionGate,
    Contradiction,
    ContradictionSeverity,
    RootCauseVerificationPolicy,
    VerificationEngine,
    VerificationStatus,
    rank_hypotheses,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.hypothesis.models import Hypothesis, HypothesisStatus
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


def test_rank_prefers_supported_over_proposed():
    a = _hyp("first", HypothesisStatus.PROPOSED)
    b = _hyp("second", HypothesisStatus.SUPPORTED)
    ranked = rank_hypotheses([a, b])
    assert ranked[0].statement == "second"


def test_rank_rejected_last():
    a = _hyp("bad", HypothesisStatus.REJECTED)
    b = _hyp("new", HypothesisStatus.PROPOSED)
    ranked = rank_hypotheses([a, b])
    assert ranked[-1].statement == "bad"


def test_unresolved_critical_contradiction_contradicts():
    engine = VerificationEngine()
    result = engine.evaluate_hypothesis(
        _hyp(),
        [_ev() for _ in range(6)],
        [Contradiction(statement="nope", severity=ContradictionSeverity.CRITICAL)],
        [],
        RootCauseVerificationPolicy(),
    )
    assert result.status == VerificationStatus.CONTRADICTED


def test_gate_requires_causal_chain_when_policy_demands():
    import asyncio

    async def _run():
        gate = ConclusionGate()
        engine = VerificationEngine()
        verification = engine.evaluate_hypothesis(
            _hyp(), [_ev() for _ in range(6)], [], [], RootCauseVerificationPolicy()
        )
        decision = await gate.evaluate(
            "tenant-a",
            uuid4(),
            verification,
            [],
            [],
            [_ev()],
            RootCauseVerificationPolicy(),
        )
        return decision

    decision = asyncio.run(_run())
    assert decision.approved is False
    assert any("causal" in b for b in decision.blockers)


def test_gate_approves_with_causal_chain_and_diverse_bound_evidence():
    import asyncio

    async def _run():
        gate = ConclusionGate()
        engine = VerificationEngine()
        items = [
            _ev("prov-a", EvidenceType.LOG),
            _ev("prov-a", EvidenceType.LOG),
            _ev("prov-b", EvidenceType.DATABASE_STATE),
            _ev("prov-b", EvidenceType.DATABASE_STATE),
        ]
        chain = [
            CausalRelationship(
                cause="c",
                mechanism="m",
                effect="e",
                confidence=0.7,
                evidence_ids=[items[0].evidence_id],
            )
        ]
        verification = engine.evaluate_hypothesis(
            _hyp(), items, [], chain, RootCauseVerificationPolicy()
        )
        decision = await gate.evaluate(
            "tenant-a",
            uuid4(),
            verification,
            [],
            chain,
            items,
            RootCauseVerificationPolicy(),
        )
        return decision

    decision = asyncio.run(_run())
    assert decision.approved is True


class TestConcessionDetector:
    def test_conceding_statements_rejected(self):
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )

        for text in [
            "The full call graph is not available due to missing extraction",
            "Root cause unknown; could not determine the failure",
            "Unable to verify the hypothesis",
            "No data available to establish causation",
        ]:
            assert VerificationEngine.concedes_claim(text) is True, text

    def test_legitimate_eliminations_pass(self):
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )

        for text in [
            "The latency is not caused by Elasticsearch delays",
            "Pool exhaustion at checkout under load",
            "Missing profile revision data causes empty listings",
            "",
        ]:
            assert VerificationEngine.concedes_claim(text) is False, text

    def test_conceding_hypothesis_never_verifies(self):
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
            VerificationStatus,
        )
        from investigation_agent_platform.domain.hypothesis.models import Hypothesis

        engine = VerificationEngine()
        hyp = Hypothesis(
            tenant_id="t",
            investigation_id=__import__("uuid").uuid4(),
            title="t",
            description="d",
            statement="The graph is not available due to incomplete extraction",
        )
        result = engine.evaluate_hypothesis(hyp, [], [], [], object())
        assert result.status == VerificationStatus.INCONCLUSIVE
        assert result.corroborating_evidence_ids == []


class TestDefinitionClaimRequiresRangeRead:
    def _evidence(self, operation):
        from datetime import UTC, datetime
        from uuid import uuid4

        from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
        from investigation_agent_platform.domain.provenance.models import (
            EvidenceFreshness,
            EvidenceProvenance,
            QueryFingerprint,
            SourceLocation,
        )

        iid = uuid4()
        now = datetime.now(UTC)
        return Evidence(
            tenant_id="t",
            investigation_id=iid,
            evidence_type=EvidenceType.SOURCE_CODE,
            provider="test",
            source="test://src",
            title="t",
            summary="s",
            content_snippet="snippet",
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
                    provider_type="test", operation=operation, normalized_query_hash="h"
                ),
                source_location=SourceLocation(system="test", identifier="x"),
            ),
            freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
            fingerprint=f"fp-{uuid4()}",
        )

    def _hyp(self, statement):
        from uuid import uuid4

        from investigation_agent_platform.domain.hypothesis.models import Hypothesis

        return Hypothesis(
            tenant_id="t",
            investigation_id=uuid4(),
            title="t",
            description="d",
            statement=statement,
        )

    def test_search_only_definition_claim_blocked(self):
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
            VerificationStatus,
        )

        engine = VerificationEngine()
        hyp = self._hyp("The widget is defined in src/widgets.py")
        result = engine.evaluate_hypothesis(hyp, [self._evidence("SEARCH_CODE")], [], [], object())
        assert result.status == VerificationStatus.INCONCLUSIVE
        assert "range-read" in result.explanation

    def test_range_read_definition_claim_passes_check(self):
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )

        engine = VerificationEngine()
        assert engine.has_range_read([self._evidence("GET_SOURCE")]) is True
        assert engine.has_range_read([self._evidence("SEARCH_CODE")]) is False
        assert VerificationEngine.claims_definition("X is defined in Y") is True
        assert VerificationEngine.claims_definition("X is located near Y") is False

    def test_non_definition_claim_unaffected(self):
        from types import SimpleNamespace

        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )

        policy = SimpleNamespace(
            max_allowed_unresolved_contradictions=0,
            require_cross_layer_evidence=False,
            min_confidence_threshold=0.8,
        )
        hyp = self._hyp("Pool exhaustion causes latency")
        # Reaches scoring (no early INCONCLUSIVE from claim-text checks).
        result = VerificationEngine().evaluate_hypothesis(hyp, [], [], [], policy)
        assert "range-read" not in result.explanation


def _code_evidence(operation="GET_SOURCE", with_location=True, etype=None):
    from datetime import UTC, datetime
    from uuid import uuid4

    from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
    from investigation_agent_platform.domain.provenance.models import (
        EvidenceFreshness,
        EvidenceProvenance,
        QueryFingerprint,
        SourceLocation,
    )

    iid = uuid4()
    now = datetime.now(UTC)
    location = (
        SourceLocation(system="git", identifier="x", file_path="src/a.py",
                       line_start=10, line_end=20, revision="abc123")
        if with_location
        else SourceLocation(system="git", identifier="x")
    )
    return Evidence(
        tenant_id="t",
        investigation_id=iid,
        evidence_type=etype or EvidenceType.SOURCE_CODE,
        provider="test",
        source="test://src",
        title="t",
        summary="s",
        content_snippet="snippet",
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
                provider_type="test", operation=operation, normalized_query_hash="h"
            ),
            source_location=location,
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=f"fp-{uuid4()}",
    )


def _loc_hyp(statement):
    from uuid import uuid4

    from investigation_agent_platform.domain.hypothesis.models import Hypothesis

    return Hypothesis(
        tenant_id="t",
        investigation_id=uuid4(),
        title="t",
        description="d",
        statement=statement,
    )


class TestLocationAnswerPath:
    def test_definition_with_range_read_validates(self):
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
            VerificationStatus,
        )

        engine = VerificationEngine()
        hyp = _loc_hyp("The widget is defined in src/widgets.py")
        result = engine.validate_location_claim(hyp, [_code_evidence("GET_SOURCE")], [])
        assert result is not None
        assert result.status == VerificationStatus.VERIFIED
        assert result.location_validated is True
        assert len(result.corroborating_evidence_ids) == 1

    def test_listing_with_call_graph_validates(self):
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
            VerificationStatus,
        )

        engine = VerificationEngine()
        hyp = _loc_hyp("The callers of f are g and h")
        ev = _code_evidence("SEARCH_CODE", etype=None)
        from investigation_agent_platform.domain.evidence.models import EvidenceType

        ev = ev.model_copy(update={"evidence_type": EvidenceType.CALL_GRAPH})
        result = engine.validate_location_claim(hyp, [ev], [])
        assert result is not None
        assert result.status == VerificationStatus.VERIFIED

    def test_non_location_claim_returns_none(self):
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )

        engine = VerificationEngine()
        assert (
            engine.validate_location_claim(_loc_hyp("Pool exhaustion causes latency"), [_code_evidence()], [])
            is None
        )

    def test_conceding_location_claim_returns_none(self):
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )

        engine = VerificationEngine()
        assert (
            engine.validate_location_claim(
                _loc_hyp("Callers unavailable due to missing extraction"), [_code_evidence()], []
            )
            is None
        )

    def test_missing_provenance_returns_none(self):
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )

        engine = VerificationEngine()
        assert (
            engine.validate_location_claim(
                _loc_hyp("X is located in src/a.py"), [_code_evidence(with_location=False)], []
            )
            is None
        )

    def test_gate_skips_causal_only_when_flagged(self):
        import asyncio as _asyncio
        from uuid import uuid4

        from investigation_agent_platform.application.investigation.verification import (
            ConclusionGate,
            RootCauseVerificationPolicy,
            VerificationEngine,
        )

        async def _run():
            engine = VerificationEngine()
            hyp = _loc_hyp("X is defined in src/a.py")
            result = engine.validate_location_claim(hyp, [_code_evidence("GET_SOURCE")], [])
            assert result is not None
            gate = ConclusionGate()
            policy = RootCauseVerificationPolicy()
            approved = await gate.evaluate(
                "t", uuid4(), result, [], [], [_code_evidence("GET_SOURCE")],
                policy, location_validated=True,
            )
            denied = await gate.evaluate(
                "t", uuid4(), result, [], [], [_code_evidence("GET_SOURCE")], policy
            )
            return approved.approved, denied.approved, denied.blockers

        approved, denied, blockers = _asyncio.run(_run())
        assert approved is True
        assert denied is False
        assert any("causal" in b for b in blockers)


def _code_evidence_at_path(file_path, operation="SEARCH_CODE"):
    from investigation_agent_platform.domain.provenance.models import SourceLocation

    ev = _code_evidence(operation)
    loc = SourceLocation(
        system="git", identifier="x", file_path=file_path,
        line_start=10, line_end=20, revision="abc123",
    )
    prov = ev.provenance.model_copy(update={"source_location": loc})
    return ev.model_copy(update={"provenance": prov})


class TestCorrectedPathClaims:
    G9_STATEMENT = (
        "The file src/investigation_agent_platform/application/worker/activites.py "
        "does not exist; the correct file is "
        "src/investigation_agent_platform/application/worker/activities.py, "
        "and its contents are available."
    )
    CORRECTED = "src/investigation_agent_platform/application/worker/activities.py"

    def test_corrected_path_naming_cited_file_validates(self):
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
            VerificationStatus,
        )

        engine = VerificationEngine()
        result = engine.validate_location_claim(
            _loc_hyp(self.G9_STATEMENT), [_code_evidence_at_path(self.CORRECTED)], []
        )
        assert result is not None
        assert result.status == VerificationStatus.VERIFIED
        assert result.location_validated is True
        assert self.CORRECTED in result.explanation

    def test_typo_path_alone_never_matches(self):
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )

        engine = VerificationEngine()
        # Statement names only the nonexistent path; cited file is unrelated.
        assert (
            engine.validate_location_claim(
                _loc_hyp("The file src/missing/activites.py does not exist."),
                [_code_evidence()],
                [],
            )
            is None
        )

    def test_correction_without_cited_evidence_returns_none(self):
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )

        engine = VerificationEngine()
        assert (
            engine.validate_location_claim(
                _loc_hyp(self.G9_STATEMENT),
                [_code_evidence(with_location=False)],
                [],
            )
            is None
        )

    def test_conceding_correction_still_blocked(self):
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )

        engine = VerificationEngine()
        assert (
            engine.validate_location_claim(
                _loc_hyp(
                    "Could not determine the right file; it might be src/a.py."
                ),
                [_code_evidence()],
                [],
            )
            is None
        )

    def test_statement_path_extraction(self):
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )

        paths = VerificationEngine._statement_paths(self.G9_STATEMENT)
        assert self.CORRECTED in paths
        assert (
            "src/investigation_agent_platform/application/worker/activites.py"
            in paths
        )
        # Bare words and versions never match (slash required).
        assert VerificationEngine._statement_paths("Pool exhaustion v1.2 causes latency") == []

    def test_locator_qualified_citation_matches_repo_relative_claim(self):
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
            VerificationStatus,
        )

        engine = VerificationEngine()
        # Live shape: providers cite org/repo-qualified paths while claims
        # name repo-relative ones (reprocess 0187a83b evidence rows).
        ev = _code_evidence_at_path("balajit/investigation-agent-platform/" + self.CORRECTED)
        result = engine.validate_location_claim(_loc_hyp(self.G9_STATEMENT), [ev], [])
        assert result is not None
        assert result.status == VerificationStatus.VERIFIED
        assert self.CORRECTED in result.explanation

    def test_suffix_match_is_segment_aligned(self):
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )

        # "otheractivities.py" must not match via ".../activities.py" suffix.
        assert (
            VerificationEngine._names_cited_path(
                "The correct file is src/worker/otheractivities.py.",
                [_code_evidence_at_path("balajit/repo/src/worker/activities.py")],
            )
            is None
        )


class TestSelectVerifiableCandidate:
    def _hyp(self, hid, status, supporting):
        from uuid import uuid4

        from investigation_agent_platform.domain.hypothesis.models import Hypothesis

        return Hypothesis(
            tenant_id="t",
            investigation_id=uuid4(),
            id=hid,
            title="t",
            description="d",
            statement="s",
            status=status,
            supporting_evidence_ids=supporting,
        )

    def test_prefers_resolvable_citations_over_stale_first(self):
        from uuid import uuid4

        from investigation_agent_platform.application.investigation.verification import (
            rank_hypotheses,
            select_verifiable_candidate,
        )

        stale_id, live_id, missing = uuid4(), uuid4(), uuid4()
        stale = self._hyp(stale_id, "UNDER_INVESTIGATION", [missing])
        live = self._hyp(live_id, "UNDER_INVESTIGATION", [live_id])
        ranked = rank_hypotheses([stale, live])
        assert ranked[0].id == stale_id  # stale copy sorts first (older)
        chosen = select_verifiable_candidate(ranked, {live_id: object()})
        assert chosen is not None and chosen.id == live_id

    def test_falls_back_to_head_eligible_when_nothing_resolves(self):
        from uuid import uuid4

        from investigation_agent_platform.application.investigation.verification import (
            rank_hypotheses,
            select_verifiable_candidate,
        )

        first = self._hyp(uuid4(), "UNDER_INVESTIGATION", [uuid4()])
        second = self._hyp(uuid4(), "UNDER_INVESTIGATION", [])
        ranked = rank_hypotheses([first, second])
        assert select_verifiable_candidate(ranked, {}).id == ranked[0].id

    def test_skips_rejected_and_contradicted(self):
        from uuid import uuid4

        from investigation_agent_platform.application.investigation.verification import (
            select_verifiable_candidate,
        )

        bad = self._hyp(uuid4(), "REJECTED", [])
        good_id = uuid4()
        good = self._hyp(uuid4(), "UNDER_INVESTIGATION", [good_id])
        assert select_verifiable_candidate([bad, good], {good_id: object()}).id == good.id
        assert select_verifiable_candidate([bad, good], {}) is not None
        assert select_verifiable_candidate([bad], {}) is None
